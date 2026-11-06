"""Bounded chart previews captured with a diagnosis, never refetched later."""
import numpy as np

from corca.topology import metric_kind, service_of


def capture_evidence(frame, source, inject_time):
    times=frame['time'].to_numpy(float)
    indices=sorted(set([0,len(frame)-1,int(np.searchsorted(times,inject_time).clip(0,len(frame)-1)),
                        *np.linspace(0,len(frame)-1,min(117,len(frame)),dtype=int).tolist()]))
    series=[];seen=set()
    for column in frame.columns:
        if column=='time': continue
        service=service_of(column)
        if service in seen: continue
        seen.add(service)
        values=frame[column].to_numpy(float)
        normal=values[(times<inject_time)&np.isfinite(values)]
        missing=~np.isfinite(values)
        starts=np.flatnonzero(missing & np.r_[True,~missing[:-1]])
        ends=np.flatnonzero(missing & np.r_[~missing[1:],True])
        series.append({'service':service,'metric':metric_kind(column),
            'baseline':np.quantile(normal,[.25,.75]).tolist() if len(normal) else None,
            'points':[[float(times[i]),float(values[i]) if np.isfinite(values[i]) else None] for i in indices],
            'gaps':[[float(times[a]),float(times[b])] for a,b in list(zip(starts,ends))[:100]],
            'gaps_truncated':len(starts)>100})
    return {'available':True,'series':series,'edges':source.get('edges',[]),
            'samples':len(frame),'preview_points':len(indices),
            'note':'Captured at analysis time. One metric per service, sampled to at most 120 points; baseline band is the pre-incident interquartile range. Gaps are recorded separately.'}
