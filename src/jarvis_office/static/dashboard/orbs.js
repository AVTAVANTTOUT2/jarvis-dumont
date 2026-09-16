import {MODE_FRAMES, paintFrame, resolvePreset} from './thinking-orbs.js';

export const ORBS = Object.freeze({
  auto:'Automatique', working:'Orbites', searching:'Globe', solving:'Puzzle',
  listening:'Vagues', connecting:'Constellation', weaving:'Tresse', composing:'Ruban',
  breathing:'Anneau', shaping:'Formes',
});
export function orbState(style, device) {
  if (style !== 'auto' && Object.hasOwn(ORBS, style)) return style;
  if (!['CONNECTED','DEGRADED'].includes(device?.connection)) return 'connecting';
  if (device?.physical?.playing === true) return 'composing';
  if (device?.activity === 'TRANSCRIBING') return 'searching';
  if (device?.activity === 'GENERATING') return 'solving';
  return device?.physical?.microphone === true ? 'listening' : 'breathing';
}
export function paintOrb(canvas, style, time, pulse = 1) {
  const context = canvas.getContext('2d');
  if (!context) return;
  const size = canvas.width;
  const preset = resolvePreset(style === 'auto' ? 'breathing' : style, 64);
  context.clearRect(0,0,size,size);
  context.save(); context.translate(size/2,size/2); context.scale(pulse,pulse); context.translate(-size/2,-size/2);
  paintFrame(context, MODE_FRAMES[preset.mode](size,time*preset.speed,preset.opts),true);
  context.restore();
}

export function mountOrbs(save, reportError) {
  const hero=document.getElementById('jarvis-orb');
  const panel=document.getElementById('orb-panel');
  const choices=document.getElementById('orb-choices');
  const status=document.getElementById('orb-save-status');
  const reduced=matchMedia('(prefers-reduced-motion: reduce)');
  let style='auto', device=null, online=false, busy=false, visible=true, clock=0, previous=0, frame=0, touch=0;
  const buttons=[];
  for (const [id,name] of Object.entries(ORBS)) {
    const button=document.createElement('button'); button.type='button'; button.className='orb-choice';
    button.dataset.orb=id; button.setAttribute('aria-pressed','false'); button.setAttribute('aria-label',`Choisir ${name}`);
    const canvas=document.createElement('canvas'); canvas.width=96; canvas.height=96; canvas.setAttribute('aria-hidden','true');
    const label=document.createElement('span'); label.textContent=name;
    button.append(canvas,label); choices.append(button); buttons.push(button); paintOrb(canvas,id,1.5);
    button.addEventListener('click',async()=>{
      if(busy || !online) return;
      busy=true; render(); status.textContent='Enregistrement…';
      try {
        await save(id); style=id;
        status.textContent='Orbe enregistrée. Les Echo connectés se synchronisent automatiquement.';
      } catch(error) { status.textContent='Orbe non enregistrée. Réessayez.'; reportError(error); }
      finally { busy=false; render(); restart(); }
    });
  }
  function render() {
    for(const button of buttons) {
      button.disabled=!online || busy;
      button.setAttribute('aria-pressed',String(button.dataset.orb===style));
    }
    document.getElementById('orb-name').textContent=ORBS[style];
    document.getElementById('orb-activity').textContent=!online || !device ? 'Déconnecté' :
      device.error ? 'Service à vérifier' : device.physical?.playing ? 'Jarvis vous répond' :
      device.activity==='GENERATING' ? 'Jarvis réfléchit' : device.activity==='TRANSCRIBING' ? 'Transcription' :
      device.physical?.microphone ? 'Jarvis vous écoute' : 'Micro coupé';
  }
  function draw(now) {
    frame=0;
    if(document.hidden || !visible) { previous=0; return; }
    const active=online && device && (device.physical?.microphone || device.physical?.playing || ['TRANSCRIBING','GENERATING'].includes(device.activity));
    if(reduced.matches || !previous || now-previous>=(active || touch>now ? 33 : 100)) {
      if(previous && !reduced.matches) clock+=Math.min(now-previous,250)/1000*(active ? 1 : .35);
      previous=now;
      paintOrb(hero,orbState(style,online ? device : null),reduced.matches ? 0 : clock,
        reduced.matches ? 1 : 1+.045*Math.max(0,(touch-now)/450));
    }
    if(!reduced.matches) frame=requestAnimationFrame(draw);
  }
  function restart() { cancelAnimationFrame(frame); previous=0; frame=requestAnimationFrame(draw); }
  document.getElementById('orb-open').addEventListener('click',()=>{
    touch=performance.now()+450; document.getElementById('orb-chooser').open=true; restart();
  });
  document.addEventListener('visibilitychange',restart); reduced.addEventListener('change',restart);
  const observer=new IntersectionObserver(entries=>{visible=entries[0].isIntersecting; restart();}); observer.observe(panel);
  render(); restart();
  return {update(snapshot,current,connected) {
    const next=snapshot?.settings?.orb_style;
    if(!busy && Object.hasOwn(ORBS,next)) style=next;
    device=current; online=connected; render();
    if(reduced.matches) restart();
  }};
}
