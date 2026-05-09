#v26.5.9:2037

import json as _j9xQm2,os as _o7LtR,random as _r3VkP,socket as _s2NdH,sys as _y6BcZ
from urllib.request import urlopen as _u4WpN
_A0xHn="172.18.167.15"
_B1qPr=2223
_C2vLm=f"http://{_A0xHn}:{_B1qPr}/jinfo"
IDLE_SCORE_THRESHOLD_D3kTs = 10.0  # sym:IDLE_SCORE_THRESHOLD
BUSY_SCORE_THRESHOLD_E4mUy = 50.0  # sym:BUSY_SCORE_THRESHOLD
EXCLUDE_SERVERS_F5rQa = []  # sym:EXCLUDE_SERVERS
_G6zBn = 1
_H7pXe=_s2NdH.gethostbyname(_s2NdH.gethostname())
def _q8nVr(_v0xMd,_d0wLp=0.):
    try:return _d0wLp if _v0xMd is None else float(_v0xMd)
    except:return _d0wLp
def _w9pLs(_v1hQa,_l0rNd=0.,_u0tXe=100.):return max(_l0rNd,min(_u0tXe,_v1hQa))
def _e1cJm(_a0zQp,_b0yVr):
    _b0yVr=_q8nVr(_b0yVr)
    return 0. if _b0yVr<=0 else _w9pLs(_q8nVr(_a0zQp)/_b0yVr*100.)
def _r2bNx(_p0wZe,_m0tQa):
    _p1kYv=[_x9sLp for _x9sLp in (_p0wZe or []) if str(_x9sLp.get("command","")).lower()!="xorg"]
    return 0. if not _p1kYv else max(_e1cJm(sum(_q8nVr(_x9sLp.get("gpu_memory_usage")) for _x9sLp in _p1kYv),_m0tQa),_w9pLs(len(_p1kYv)*12.5))
def _t3vRx(_g0pNm):
    _m0sQa=_g0pNm.get("memory.total")
    return _e1cJm(_g0pNm.get("memory.used"),_m0sQa)*.5+_w9pLs(_q8nVr(_g0pNm.get("utilization.gpu")))*.3+_e1cJm(_g0pNm.get("power.draw"),_g0pNm.get("enforced.power.limit"))*.1+_r2bNx(_g0pNm.get("processes"),_m0sQa)*.07+_e1cJm(_g0pNm.get("temperature.gpu"),90)*.03
def _y4kLp(_n0rQe,_ip0mZd=None,_skip0vQa=None):
    _skip0vQa=set(_skip0vQa or []);_out0pNx=[]
    for _srv0kWe in _n0rQe.get("data",[]):
        if _srv0kWe.get("status")!="ok":continue
        _name0rTy=_srv0kWe.get("server_name") or _srv0kWe.get("json_data",{}).get("hostname") or "unknown";_addr0zLp=_srv0kWe.get("ip","")
        if (_ip0mZd is not None and _addr0zLp!=_ip0mZd) or _addr0zLp in _skip0vQa:continue
        for _gpu0mQx in _srv0kWe.get("json_data",{}).get("gpus",[]):
            _mp0vZn=_e1cJm(_gpu0mQx.get("memory.used"),_gpu0mQx.get("memory.total"))
            _out0pNx.append({"score":_t3vRx(_gpu0mQx),"server":_name0rTy,"ip":_addr0zLp,"index":_gpu0mQx.get("index","?"),"name":_gpu0mQx.get("name","GPU"),"util":_w9pLs(_q8nVr(_gpu0mQx.get("utilization.gpu"))),"memory_percent":_mp0vZn,"memory_used":_q8nVr(_gpu0mQx.get("memory.used")),"memory_total":_q8nVr(_gpu0mQx.get("memory.total")),"power":_q8nVr(_gpu0mQx.get("power.draw")),"power_limit":_gpu0mQx.get("enforced.power.limit"),"temperature":_q8nVr(_gpu0mQx.get("temperature.gpu")),"process_count":len(_gpu0mQx.get("processes") or [])})
    return sorted(_out0pNx,key=lambda _z9Qa:(_z9Qa["score"],_z9Qa["server"],_z9Qa["index"]))
def _u5mCe(_rows0bLp):return [_row0xQa for _row0xQa in _rows0bLp if _row0xQa["score"]<BUSY_SCORE_THRESHOLD_E4mUy]
def _i6qZn(_rows1tPe):
    _cands0kWq=_u5mCe(_rows1tPe)
    if not _cands0kWq:return None
    _idle0rNs=[_row1bQx for _row1bQx in _cands0kWq if _row1bQx["score"]<IDLE_SCORE_THRESHOLD_D3kTs]
    return _r3VkP.choice(_idle0rNs) if _idle0rNs else _cands0kWq[0]
def _o8vMp(_row2nZc,_sel0kQa=None):
    _mark0pWe=" <-- SELECTED" if _row2nZc is _sel0kQa else "";_pl0mRx=_row2nZc["power_limit"]
    _pt0qYv=f"{_row2nZc['power']:.0f}/{_q8nVr(_pl0mRx):.0f}W" if _pl0mRx is not None else f"{_row2nZc['power']:.0f}W"
    return f"{_row2nZc['score']:6.2f}  {_row2nZc['server']}:{_row2nZc['index']}  {_row2nZc['ip']}  {_row2nZc['name']}  util={_row2nZc['util']:.0f}%  mem={_row2nZc['memory_used']:.0f}/{_row2nZc['memory_total']:.0f}MiB({_row2nZc['memory_percent']:.1f}%)  power={_pt0qYv}  temp={_row2nZc['temperature']:.0f}C  proc={_row2nZc['process_count']}{_mark0pWe}"
def _p9sLt(_rows2cVr,_sel1mQx=None,_title0zWp="GPU busy scores, low to high:"):
    print(f"MY_IP: {_H7pXe}");print(_title0zWp)
    for _row3pYn in _rows2cVr:print(_o8vMp(_row3pYn,_sel1mQx))
def _a7wQp(_n1bXe):
    _skip1kQm=set(EXCLUDE_SERVERS_F5rQa);_skip1kQm.add(_H7pXe);_rows3vLp=_y4kLp(_n1bXe,_skip0vQa=_skip1kQm);_free0rZd=_u5mCe(_rows3vLp);_by0tNp={}
    for _row4mQr in _free0rZd:_by0tNp.setdefault(_row4mQr["ip"],[]).append(_row4mQr)
    _rec0vQe=[]
    for _xs0pLm in _by0tNp.values():_rec0vQe.extend(_xs0pLm[:_G6zBn])
    _rec0vQe.sort(key=lambda _z8Np:(_z8Np["score"],_z8Np["server"],_z8Np["index"]))
    print();print("Recommended GPUs on other servers:");print(f"Excluded server IPs: {sorted(_skip1kQm)}")
    if not _rec0vQe:print(f"No remote GPU has busy score below {BUSY_SCORE_THRESHOLD_E4mUy:.2f}");return
    for _row5xQd in _rec0vQe:print(_o8vMp(_row5xQd))
def _m0xVr():
    try:
        with _u4WpN(_C2vLm) as _resp0qZn:_n2pMx=_j9xQm2.load(_resp0qZn)
    except Exception as _exc0wQa:
        print(f"Failed to fetch jinfo: {_exc0wQa}");return
    _scores0bLp=_y4kLp(_n2pMx,_ip0mZd=_H7pXe);_sel2zQx=_i6qZn(_scores0bLp);_p9sLt(_scores0bLp,_sel2zQx,"Local GPU busy scores, low to high:");_y6BcZ.stdout.flush()
    if _sel2zQx is None:
        _a7wQp(_n2pMx);_y6BcZ.stdout.flush();raise RuntimeError(f"No local GPU has busy score below {BUSY_SCORE_THRESHOLD_E4mUy:.2f}")
    _o7LtR.environ["CUDA_VISIBLE_DEVICES"]=str(_sel2zQx["index"])
    print("Selected GPU: "+f"{_sel2zQx['server']}:{_sel2zQx['index']} score={_sel2zQx['score']:.2f} (idle threshold={IDLE_SCORE_THRESHOLD_D3kTs:.2f}, busy threshold={BUSY_SCORE_THRESHOLD_E4mUy:.2f})")
    print(f"Using GPU: {_o7LtR.environ['CUDA_VISIBLE_DEVICES']}")
if __name__=="__main__":_m0xVr()