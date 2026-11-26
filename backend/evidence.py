"""Bounded chart previews captured with a diagnosis, never refetched later."""
import numpy as np

from corca.topology import metric_kind, service_of
from corca.base import anomaly_scores


def capture_evidence(frame, source, inject_time, modalities=None):
    times=frame['time'].to_numpy(float)
    indices=sorted(set([0,len(frame)-1,int(np.searchsorted(times,inject_time).clip(0,len(frame)-1)),
                        *np.linspace(0,len(frame)-1,min(117,len(frame)),dtype=int).tolist()]))
    series=[];seen=set()
    columns = list(frame.columns)
    if modalities is not None:
        scores = anomaly_scores(frame, inject_time)
        columns.sort(key=lambda column: scores.get(column, float('-inf')), reverse=True)
    for column in columns:
        if column=='time': continue
        service=service_of(column)
        key=(service,modalities.get(column,'metrics')) if modalities is not None else service
        if key in seen: continue
        seen.add(key)
        values=frame[column].to_numpy(float)
        normal=values[(times<inject_time)&np.isfinite(values)]
        missing=~np.isfinite(values)
        starts=np.flatnonzero(missing & np.r_[True,~missing[:-1]])
        ends=np.flatnonzero(missing & np.r_[~missing[1:],True])
        series.append({'service':service,'metric':metric_kind(column),
            **({'modality':modalities.get(column,'metrics')} if modalities is not None else {}),
            'baseline':np.quantile(normal,[.25,.75]).tolist() if len(normal) else None,
            'points':[[float(times[i]),float(values[i]) if np.isfinite(values[i]) else None] for i in indices],
            'gaps':[[float(times[a]),float(times[b])] for a,b in list(zip(starts,ends))[:100]],
            'gaps_truncated':len(starts)>100})
    return {'available':True,'series':series,'edges':source.get('edges',[]),
            'samples':len(frame),'preview_points':len(indices),
            'note':'Captured at analysis time. '+('Strongest anomalous stream per service and modality' if modalities is not None else 'One metric per service')+', sampled to at most 120 points; baseline band is the pre-incident interquartile range. Gaps are recorded separately.'}
