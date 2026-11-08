import csv, sys, collections
f = sys.argv[1]
r = csv.reader(open(f, encoding='latin-1')); h = next(r); print(len(h), 'cols:', h)
rows = list(r); print('rows', len(rows)); ix = {c: i for i, c in enumerate(h)}
def cnt(c, k=8): print(c, collections.Counter(x[ix[c]] for x in rows).most_common(k))
for c in ['STATENAME', 'RUR_URBNAME', 'FUNC_SYSNAME', 'HARM_EVNAME', 'MAN_COLLNAME', 'WEATHERNAME', 'LGT_CONDNAME']:
    if c in ix: cnt(c)
ny = [x for x in rows if x[ix['STATENAME']] == 'New York' and x[ix['RUR_URBNAME']] == 'Urban']
print('NY urban', len(ny)); print(dict(zip(h, rows[0])))
