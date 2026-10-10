/* Private metadata and controls only. Media is streamed from the server. */
let libraryItems = [];
let memberManifests = [];
let playingItem = null;
let lastPositionWrite = 0;
let currentView = 'quick';

window.api = async (path, options = {}) => {
  const res = await fetch(path, {cache:'no-store', ...options, headers:{'Content-Type':'application/json', ...(options.headers || {})}});
  const data = await res.json();
  if (!res.ok) {
    if (res.status === 401) showLogin('Sign in to continue.');
    throw new Error(typeof data.detail === 'string' ? data.detail : 'Request failed.');
  }
  return data;
};
const jsonBody = value => JSON.stringify(value);
function message(id, text) { $(id).textContent = text; }
async function perform(id, action) {
  try { await action(); } catch(error) { message(id,error.message); }
}
function showLogin(text = '') {
  $('loginPanel').classList.remove('hidden'); $('navigation').hidden = true;
  document.querySelectorAll('[data-panel]').forEach(panel=>panel.classList.add('hidden'));
  message('loginMessage',text);
}
window.showView = view => {
  currentView=view;
  document.querySelectorAll('[data-panel]').forEach(panel=> {
    const visible = panel.dataset.panel===view && (panel.id!=='result' || panel.dataset.ready==='1');
    panel.classList.toggle('hidden',!visible);
  });
  document.querySelectorAll('[data-view]').forEach(button=>{ button.className=button.dataset.view===view?'primary':'ghost'; button.setAttribute('aria-current',button.dataset.view===view?'page':'false'); });
  if (view==='library') perform('libraryMessage',refreshLibrary);
  if (view==='settings') perform('settingsMessage',refreshSettings);
  if (view==='membership') perform('membershipMessage',refreshMembers);
  if (view==='history') refreshHistory();
};
async function boot() {
  const status = await window.api('/api/auth/status');
  if (!status.authenticated) return showLogin(status.configured?'':'Owner password needs server setup. See the development README.');
  $('loginPanel').classList.add('hidden'); $('navigation').hidden=false;
  const preferences=await window.api('/api/settings');
  $('destination').value=preferences.destination || 'library'; $('batchDestination').value=$('destination').value;
  $('preferredDestination').value=$('destination').value;
  $('preferredQuality').value=String(preferences.quality || 0); $('batchQuality').value=$('preferredQuality').value;
  window.showView('quick'); refreshHistory(true);
}
$('loginForm').addEventListener('submit',event=>{event.preventDefault();perform('loginMessage',async()=>{
  const password=$('ownerPassword').value; $('ownerPassword').value='';
  await window.api('/api/auth/login',{method:'POST',body:jsonBody({password})}); await boot();
});});
document.querySelectorAll('[data-view]').forEach(button=>button.addEventListener('click',()=>window.showView(button.dataset.view)));

$('queueBatch').addEventListener('click',()=>perform('batchMessage',async()=>{
  const urls=$('batchUrls').value.split(/\n/).map(x=>x.trim()).filter(Boolean);
  if (!urls.length || urls.length>100) throw new Error('Enter 1–100 URLs, one per line.');
  const destination=$('batchDestination').value, quality=Number($('batchQuality').value), account=$('batchAccount').value;
  const response=await window.api('/api/batch',{method:'POST',body:jsonBody({items:urls.map(url=>({url,destination,quality,account,filename:'download',enqueue:true}))})});
  const failed=response.results.filter(x=>x.error), duplicate=response.results.filter(x=>x.duplicate);
  message('batchMessage',`${response.results.length-failed.length-duplicate.length} queued · ${duplicate.length} duplicates · ${failed.length} rejected${failed.length?': '+failed.map(x=>`Line ${x.index+1}: ${x.error}`).join('; '):''}`); refreshHistory();
}));
for(const [id,paused] of [['pauseQueue',true],['resumeQueue',false]]) $(id).addEventListener('click',()=>perform('batchMessage',async()=>{
  await window.api('/api/queue',{method:'POST',body:jsonBody({paused})}); message('batchMessage',paused?'Queue paused. Active jobs continue.':'Queue resumed.');
}));
$('cancelQueued').addEventListener('click',()=>perform('batchMessage',async()=>{
  const data=await window.api('/api/jobs');
  for(const job of data.jobs.filter(j=>j.status==='queued')) await window.api(`/api/download/cancel/${job.job_id}`,{method:'POST'});
  message('batchMessage','Queued jobs cancelled.'); refreshHistory();
}));

function button(label, action, className='secondary compact') {
  const node=document.createElement('button'); node.type='button'; node.className=className; node.textContent=label; node.addEventListener('click',action); return node;
}
function deviceDownload(href) {
  const anchor=document.createElement('a'); anchor.href=href; anchor.rel='noopener'; document.body.append(anchor); anchor.click(); anchor.remove();
}
async function refreshLibrary() {
  const params=new URLSearchParams({q:$('librarySearch').value,sort:$('librarySort').value,collection:$('libraryCollection').value});
  if ($('libraryWatched').value) params.set('watched',$('libraryWatched').value);
  const data=await window.api('/api/library?'+params); libraryItems=data.items;
  message('libraryCount',`${libraryItems.length} item${libraryItems.length===1?'':'s'}`); const grid=$('libraryGrid'); grid.replaceChildren();
  for(const item of libraryItems) {
    const meta=item.metadata || {}, tile=document.createElement('article'); tile.className='library-tile';
    const choose=document.createElement('input'); choose.type='checkbox'; choose.dataset.libraryId=item.library_id;
    const label=document.createElement('label'); label.append(choose,document.createTextNode('Select')); tile.append(label);
    if (meta.thumbnail && /^https:\/\//i.test(meta.thumbnail)) {const img=document.createElement('img'); img.src=meta.thumbnail; img.alt=''; img.loading='lazy'; img.referrerPolicy='no-referrer'; tile.append(img);}
    const title=document.createElement('h3'); title.textContent=meta.title || item.filename;
    const detail=document.createElement('p'); detail.className='muted';
    const height=(item.validation?.streams || []).find(x=>x.codec_type==='video')?.height;
    detail.textContent=[meta.platform,meta.uploader,fmtDuration(item.validation?.duration),height?`${height}p`:null,fmtSize(item.size),item.collection,item.watched?'Watched':null].filter(Boolean).join(' · ');
    const date=document.createElement('p'); date.className='muted'; date.textContent=`Saved ${new Date(item.saved_at*1000).toLocaleDateString()} · ${item.status}`;
    const actions=document.createElement('div'); actions.className='actions';
    if(item.status==='completed') actions.append(button('Play',()=>perform('libraryMessage',()=>play(item))),button('Download to This Device',()=>deviceDownload(item.download_url)));
    actions.append(button(item.watched?'Mark unwatched':'Mark watched',()=>perform('libraryMessage',async()=>{await window.api(`/api/library/${item.library_id}`,{method:'PATCH',body:jsonBody({watched:!item.watched})});refreshLibrary();})),
      button('Collection',()=>perform('libraryMessage',async()=>{const collection=prompt('Collection name (empty to remove)',item.collection || '');if(collection!==null){await window.api(`/api/library/${item.library_id}`,{method:'PATCH',body:jsonBody({collection})});refreshLibrary();}})),
      button('Delete',()=>perform('libraryMessage',async()=>{if(!confirm(`Permanently delete “${meta.title || item.filename}” from the server library?`))return;if(playingItem?.library_id===item.library_id)closePlayer();await window.api(`/api/library/${item.library_id}?confirm=true`,{method:'DELETE'});refreshLibrary();}),'danger compact'));
    tile.append(title,detail,date,actions); grid.append(tile);
  }
  if (!libraryItems.length) grid.textContent='Your server library is empty. Save a download to Library to keep it here.';
}
async function savePosition() {
  if(!playingItem)return;
  const player=$('libraryPlayer'); const position=Number.isFinite(player.currentTime)?player.currentTime:0;
  await window.api(`/api/library/${playingItem.library_id}`,{method:'PATCH',body:jsonBody({position,watched:player.ended || playingItem.watched})});
}
function closePlayer() {
  savePosition().catch(()=>{}); const player=$('libraryPlayer'); player.pause(); player.removeAttribute('src'); player.load(); playingItem=null; $('playerPanel').classList.add('hidden');
}
async function play(item) {
  closePlayer(); playingItem=item; $('playerPanel').classList.remove('hidden');
  message('playingTitle',item.metadata?.title || item.filename);
  message('playingDetails',(item.metadata?.description || '').slice(0,500));
  const player=$('libraryPlayer'); player.src=item.play_url;
  player.onloadedmetadata=()=> { if(item.position && item.position<player.duration-5) player.currentTime=item.position; player.play().catch(()=>{}); };
  $('playerPanel').scrollIntoView({block:'nearest'});
}
$('libraryPlayer').addEventListener('timeupdate',()=>{if(Date.now()-lastPositionWrite>10000){lastPositionWrite=Date.now();savePosition().catch(()=>{});}});
$('libraryPlayer').addEventListener('pause',()=>savePosition().catch(()=>{}));
$('libraryPlayer').addEventListener('ended',()=>savePosition().catch(()=>{}));
$('libraryPlayer').addEventListener('error',()=>message('libraryMessage','This media cannot play in this browser. Download it to your device to use another player.'));
$('closePlayer').addEventListener('click',closePlayer);
$('refreshLibrary').addEventListener('click',()=>perform('libraryMessage',refreshLibrary));
$('selectLibrary').addEventListener('click',()=>document.querySelectorAll('[data-library-id]').forEach(x=>x.checked=true));
$('downloadSelected').addEventListener('click',()=>perform('libraryMessage',async()=>{
  const ids=[...document.querySelectorAll('[data-library-id]:checked')].map(x=>x.dataset.libraryId);
  for(const id of ids) {deviceDownload(libraryItems.find(x=>x.library_id===id).download_url); await sleep(400);}
  message('libraryMessage',`${ids.length} browser download requests sent. Allow multiple downloads if prompted.`);
}));
for(const id of ['librarySearch','librarySort','libraryCollection','libraryWatched']) $(id).addEventListener('change',()=>perform('libraryMessage',refreshLibrary));

async function refreshSettings() {
  const [storage,account]=await Promise.all([window.api('/api/storage'),window.api('/api/accounts')]);
  message('storageDashboard',`Library ${fmtSize(storage.library_bytes)} / ${fmtSize(storage.library_limit)} · Temporary ${fmtSize(storage.temporary_bytes)} / ${fmtSize(storage.temporary_limit)} · ${fmtSize(storage.free_bytes)} free · Queue ${storage.queue_paused?'paused':'running'}`);
  const value=account.accounts[0]; message('accountState',`YouTube: ${value.status} · ${value.verified_channels?.length || 0} channels verified for membership import${value.expires_at?' · Earliest cookie expiry '+new Date(value.expires_at*1000).toLocaleDateString():''}`);
}
$('savePreferences').addEventListener('click',()=>perform('settingsMessage',async()=>{
  await window.api('/api/settings',{method:'PUT',body:jsonBody({destination:$('preferredDestination').value,quality:Number($('preferredQuality').value)})});
  $('destination').value=$('preferredDestination').value; message('settingsMessage','Preferences saved on the server.');
}));
$('connectAccount').addEventListener('click',()=>perform('settingsMessage',async()=>{
  const file=$('cookieFile').files[0]; if(!file || file.size>200000)throw new Error('Choose a scoped cookie file under 200 KB.');
  if(!$('cookieConsent').checked)throw new Error('Confirm session ownership and authorized access.');
  try { const cookies=await file.text(); await window.api('/api/accounts/youtube',{method:'POST',body:jsonBody({cookies,authorized:true})}); }
  finally { $('cookieFile').value=''; $('cookieConsent').checked=false; }
  message('settingsMessage','Session imported. Verify it with one accessible members-only download before scanning.'); refreshSettings();
}));
$('disconnectAccount').addEventListener('click',()=>perform('settingsMessage',async()=>{await window.api('/api/accounts/youtube',{method:'DELETE'});message('settingsMessage','Server session deleted.');refreshSettings();}));
$('logout').addEventListener('click',()=>perform('settingsMessage',async()=>{closePlayer();await window.api('/api/auth/logout',{method:'POST'});showLogin();}));

function renderMembers() {
  const list=$('membershipList'); list.replaceChildren();
  for(const manifest of memberManifests) {
    const title=document.createElement('h3');title.textContent=manifest.source;list.append(title);
    const note=document.createElement('p');note.className='muted';note.textContent=`${Object.keys(manifest.entries).length} confirmed items stored · ${manifest.summary.inspected} candidates inspected in last scan · ${manifest.summary.excluded_unconfirmed} unconfirmed/public excluded. ${manifest.summary.note}`;list.append(note);
    for(const entry of Object.values(manifest.entries)) {
      if(!entry.title.toLowerCase().includes($('memberSearch').value.toLowerCase()))continue;
      const row=document.createElement('label'), check=document.createElement('input');check.type='checkbox';check.dataset.manifest=manifest.manifest_id;check.dataset.member=entry.media_key;
      row.append(check,document.createTextNode(`${entry.title} · ${entry.status}${entry.upload_date?' · '+entry.upload_date:''}`));list.append(row);
    }
  }
  if(!memberManifests.length)list.textContent='No membership manifests yet.';
}
async function refreshMembers() { const data=await window.api('/api/membership');memberManifests=data.manifests;renderMembers(); }
$('memberSearch').addEventListener('input',renderMembers);
$('scanMembership').addEventListener('click',()=>perform('membershipMessage',async()=>{
  $('scanMembership').disabled=true; message('membershipMessage','Scanning a bounded membership tab…');
  try {const data=await window.api('/api/membership/scan',{method:'POST',body:jsonBody({url:$('membershipUrl').value,limit:Number($('membershipLimit').value),account:'youtube'})});message('membershipMessage',`${Object.keys(data.entries).length} confirmed entries in manifest. Coverage is partial.`);await refreshMembers();}
  finally {$('scanMembership').disabled=false;}
}));
$('selectMembers').addEventListener('click',()=>document.querySelectorAll('[data-member]').forEach(x=>x.checked=true));
$('importMembers').addEventListener('click',()=>perform('membershipMessage',async()=>{
  const grouped={}; document.querySelectorAll('[data-member]:checked').forEach(x=>(grouped[x.dataset.manifest] ||= []).push(x.dataset.member));
  let queued=0, rejected=0;
  for(const [manifest_id,media_keys] of Object.entries(grouped)){
    const data=await window.api('/api/membership/import',{method:'POST',body:jsonBody({manifest_id,media_keys})});queued+=data.results.filter(x=>!x.error).length;rejected+=data.results.filter(x=>x.error).length;
  }
  message('membershipMessage',`${queued} selected items accepted · ${rejected} rejected. See History for progress.`);refreshMembers();refreshHistory();
}));
boot().catch(error=>showLogin(error.message));
setInterval(()=> {if(currentView==='library' && !$('navigation').hidden)perform('libraryMessage',refreshLibrary);},15000);
