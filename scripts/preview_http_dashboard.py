"""Render a synthetic Rich terminal preview without network or database access."""
import io
from pathlib import Path
import time
from rich.console import Console
from youthgames_register.terminal import Dashboard

def main():
    folder=Path('docs/evidence');folder.mkdir(parents=True,exist_ok=True)
    console=Console(file=io.StringIO(),record=True,force_terminal=True,width=138,height=50)
    with Dashboard('PREVIEW · Synthetic Raiwind run',18,enabled=False,console=console) as ui:
        ui.started=time.monotonic()-100
        ui.event('pipeline',prepare_ahead=4,max_in_flight=4,submission_interval_seconds=5,form_max_age_seconds=30)
        ui.event('queue',total=18879)
        ui.event('batch_snapshot',total=18925,completed=40,remaining=18879,held=6,
            jobs={str(n):[{'bucket':'remaining','eligible':True,'rows':1}] for n in range(1,19)})
        ui.run_started=time.monotonic()-45
        for n in range(1,19):ui.event('waiting_for_preparation',worker=n,uc=255+(n-1)%20,roll=f'TEST-{n:05d}')
        for n in range(1,8):ui.event('result',job=n,worker=n,uc=255+n-1,roll=f'TEST-{n:05d}',state='registered',tracking=f'TEST-TRACK-{n:03d}')
        ui.event('fetching_form',worker=8,uc=262,roll='TEST-00008')
        ui.event('fetching_form',worker=9,uc=263,roll='TEST-00009')
        ui.event('prepared',worker=10,uc=264,roll='TEST-00010')
        ui.event('prepared',worker=11,uc=265,roll='TEST-00011')
        ui.event('awaiting_response',worker=12,uc=266,roll='TEST-00012')
        console.print(ui.render())
    console.save_svg(str(folder/'http-dashboard-preview.svg'),title='HTTP Dashboard · Synthetic preview',clear=False)
    console.save_text(str(folder/'http-dashboard-preview.txt'))
    print(folder/'http-dashboard-preview.svg')
if __name__=='__main__':main()
