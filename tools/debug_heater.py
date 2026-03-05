#!/usr/bin/env python3
"""Pull current raw values + check ALL settings to debug the heater mystery."""
import asyncio
import struct
import websockets

async def read_all(name, ip):
    url = f"ws://{ip}/PSWS"
    ids = list(range(0, 54))  # ALL settings 0-53
    
    async with websockets.connect(url, origin="capacitor://localhost",
                                  ping_interval=None, close_timeout=5) as ws:
        readings = {}
        tx = 1
        for i in range(0, len(ids), 5):
            batch = ids[i:i+5]
            payload = b""
            for sid in batch:
                payload += struct.pack(">H", sid)
            header = struct.pack(">BHHH", 2, 3, tx, len(payload))
            await ws.send(header + payload)
            tx += 1
            await asyncio.sleep(0.3)
        
        end = asyncio.get_event_loop().time() + 15
        while asyncio.get_event_loop().time() < end:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=3)
                if isinstance(msg, bytes) and len(msg) >= 7:
                    p = msg[7:]
                    for i in range(0, len(p), 4):
                        if i+4 <= len(p):
                            sid = (p[i]<<8)|p[i+1]
                            val = (p[i+2]<<8)|p[i+3]
                            readings[sid] = val
                if len(readings) >= len(ids):
                    break
            except asyncio.TimeoutError:
                break
    
    names = {
        0:"L1(bedtime)",1:"L2(sleep)",2:"L3(wake)",3:"FootWarmLvl",
        4:"QuietEn",5:"FanLimit",6:"HeaterLimit",
        7:"BurstHotLvl",8:"BurstColdLvl",9:"BurstHotDur",10:"BurstColdDur",
        11:"Volume",12:"T1(startLen)",13:"T3(wakeLen)",14:"SchedEn",
        15:"Sched1Start",16:"Sched1Days",17:"Sched1Stop",
        18:"Sched2Start",19:"Sched2Days",20:"Sched2Stop",
        21:"Running",22:"BurstMode",23:"RunProgress",24:"BH_OUT",
        25:"Time1",26:"Time2",27:"Time3",28:"Time4",29:"Side",
        30:"TempSetpoint",31:"TA(ambient)",32:"TSR(bodyR)",33:"TSC(bodyC)",
        34:"TSL(bodyL)",35:"THH(htrHead)",36:"THF(htrFoot)",
        37:"IHH(currentH)",38:"IHF(currentF)",
        39:"BL_OUT(blower)",40:"HH_OUT(htrH%)",41:"FH_OUT(htrF%)",
        42:"CtrlOut",43:"CtrlITerm",44:"CtrlPTerm",
        45:"DL_Upload",46:"DL_Pct",47:"FW_Update",
        48:"TestFan",49:"TestHH",50:"TestHF",51:"FATinProg",
        52:"ProfileEn",53:"CoolMode"
    }
    
    print(f"\n{'=' * 65}")
    print(f"  {name} ({ip}) — ALL {len(readings)} settings")
    print(f"{'=' * 65}")
    
    for sid in sorted(readings.keys()):
        raw = readings[sid]
        n = names.get(sid, f"unknown_{sid}")
        
        if sid in (30,31,32,33,34,35,36):
            c = (raw - 32768) / 100
            f = c * 9/5 + 32
            print(f"  {sid:3d} {n:20s}  raw={raw:6d}  0x{raw:04X}  -> {f:6.1f}°F  ({c:.1f}°C)")
        elif sid in (42,43,44):
            signed = (raw - 32768) / 100
            print(f"  {sid:3d} {n:20s}  raw={raw:6d}  0x{raw:04X}  -> {signed:.2f} (signed)")
        elif sid in (0,1,2):
            display = raw - 10
            print(f"  {sid:3d} {n:20s}  raw={raw:6d}  0x{raw:04X}  -> app: {display:+d}")
        elif sid in (37,38):
            # IHH/IHF - heater current - unknown encoding
            print(f"  {sid:3d} {n:20s}  raw={raw:6d}  0x{raw:04X}  ** HEATER CURRENT **")
        elif sid == 29:
            c = chr(raw) if 32 < raw < 127 else "?"
            print(f"  {sid:3d} {n:20s}  raw={raw:6d}  0x{raw:04X}  -> '{c}'")
        else:
            print(f"  {sid:3d} {n:20s}  raw={raw:6d}  0x{raw:04X}")


async def main():
    print("CURRENT STATE OF BOTH ZONES\n")
    print("(Bed should be empty right now — compare THH/THF to room temp)")
    for name, ip in [("LEFT", "192.168.0.159"), ("RIGHT", "192.168.0.211")]:
        await read_all(name, ip)

asyncio.run(main())
