import csv, json, sys, statistics as st, collections, os
d = sys.argv[1]
rows = list(csv.DictReader(open(os.path.join(d,'events_summary.csv'))))
print('events', len(rows))
md = [float(r['max_depth_inches']) for r in rows if r['max_depth_inches']]
dur = [float(r['duration_mins']) for r in rows if r['duration_mins']]
q = lambda a,p: sorted(a)[int(p*(len(a)-1))]
print('max_depth in: median %.2f p90 %.2f p99 %.2f max %.2f' % (q(md,.5),q(md,.9),q(md,.99),max(md)))
print('duration min: median %.1f p90 %.1f max %.1f' % (q(dur,.5),q(dur,.9),max(dur)))
print('events >=4in', sum(x>=4 for x in md), '>=12in', sum(x>=12 for x in md))
days = collections.Counter(r['flood_start_time'][:10] for r in rows)
print('top flood days', days.most_common(8))
print('distinct days', len(days))
s = list(csv.DictReader(open(os.path.join(d,'sensors.csv'))))
print('sensors', len(s), collections.Counter(x['Tidally Influenced'] for x in s))
p = json.load(open(os.path.join(d,'events_profiles_sample.json')))
for e in p[:3]:
    v = json.loads(e['flood_profile_depth_inches']); t = json.loads(e['flood_profile_time_secs'])
    gaps = [b-a for a,b in zip(t,t[1:])]
    print(e['sensor_id'], e['flood_start_time'], e['max_depth_inches'], 'npts', len(v), 'median gap s', st.median(gaps), 'max gap', max(gaps))
