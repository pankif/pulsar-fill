from pathlib import Path
import sys
import dnfile
from dncil.cil.body.reader import read_method_body_from_bytes
from dncil.clr.token import Token


def dump(path):
    pe = dnfile.dnPE(str(path))
    tables = pe.net.mdtables
    names = {}
    for ti, t in enumerate(tables.TypeDef.rows, 1):
        name = f'{t.TypeNamespace}.{t.TypeName}'
        names[(2, ti)] = name
        for index in t.MethodList:
            names[(6, index.row_index)] = name + '::' + str(index.row.Name)
        for index in t.FieldList:
            names[(4, index.row_index)] = name + '::' + str(index.row.Name)

    def rowname(table, index):
        if (table, index) in names:
            return names[(table, index)]
        r = tables.tables[table].rows[index - 1]
        if table == 1:
            return f'{r.TypeNamespace}.{r.TypeName}'
        if table == 10:
            parent = r.Class
            return rowname(parent.table.number, parent.row_index) + '::' + str(r.Name)
        if table == 43:
            m = r.Method
            return rowname(m.table.number, m.row_index) + '<generic>'
        return repr(r)

    def resolve(operand):
        if not isinstance(operand, Token):
            return str(operand) if operand is not None else ''
        value = operand.value
        if value >> 24 == 0x70:
            return repr(pe.net.user_strings.get(value & 0xffffff).value)
        try:
            return f'{value:08X} ' + rowname(value >> 24, value & 0xffffff)
        except Exception:
            return f'{value:08X}'

    out = []
    for i, m in enumerate(tables.MethodDef.rows, 1):
        if not m.Rva:
            continue
        out.append(f'\nMETHOD {i:06d} TOKEN 06{i:06X} {names[(6, i)]}')
        try:
            body = read_method_body_from_bytes(pe.get_data(m.Rva, 2000000))
            for ins in body.instructions:
                out.append(f'  {ins.offset:04X} {str(ins.opcode):16s} {resolve(ins.operand)}')
        except Exception as e:
            out.append('ERROR ' + str(e))
    target = Path('work') / (path.name + '.il.txt')
    target.write_text('\n'.join(out))
    print(target, len(out), 'lines')


for name in sys.argv[1:]:
    dump(Path('work/unpacked/data/{app}') / name)
