"""Restart only the exactly revalidated owner Butler UI after an own refresh."""
from pathlib import Path
import subprocess,os,signal,time,json,datetime
expected=str(Path.home()/'Applications/Butler.app/Contents/MacOS/Butler')
rows=subprocess.check_output(['ps','-axo','pid=,args='],text=True).splitlines();stopped=[]
for line in rows:
    pid,_,args=line.strip().partition(' ')
    if args!=expected and not args.startswith(expected+' '):continue
    pid=int(pid)
    current=subprocess.run(['ps','-p',str(pid),'-o','args='],text=True,capture_output=True).stdout.strip()
    if current!=args:continue
    os.kill(pid,signal.SIGTERM);stopped.append(pid)
live=[]
for _ in range(30):
    live=[]
    for pid in stopped:
        p=subprocess.run(['ps','-p',str(pid),'-o','stat='],text=True,capture_output=True)
        if p.returncode==0 and not p.stdout.strip().startswith('Z'):live.append(pid)
    if not live:break
    time.sleep(.1)
assert not live
subprocess.run(['open','-g',str(Path.home()/'Applications/Butler.app')],check=True)
report={'at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'stopped_exact_own_ui_pids':stopped,'reopened_background':True}
Path(__file__).with_name('camera-clips-ui-restart.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report))
