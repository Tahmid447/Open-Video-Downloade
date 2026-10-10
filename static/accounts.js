/* Provider credentials enter only the explicitly authorized import, never storage or diagnostics. */
let savedYouTube = null;
let accountModeSaving = null;
function youtubeLink(value) {
  try {const url=new URL(value);return url.protocol==='https:' && ['youtube.com','www.youtube.com','m.youtube.com','youtu.be'].includes(url.hostname.toLowerCase());}
  catch {return false;}
}
function updateConnectionHint() {
  const mode=$('analysisAccount').value;
  let text='Automatic · saved connections are used only for matching providers.';
  if(mode==='public')text='Public mode · no provider session will be sent.';
  else if(youtubeLink($('url').value)) {
    if(savedYouTube?.can_attempt)text='Automatic · your saved YouTube connection will be used.';
    else if(savedYouTube?.configured)text='Your saved YouTube connection needs attention in Settings.';
    else text='No YouTube connection saved. Public extraction will be used; connect in Settings for members-only videos.';
  } else if($('url').value.trim())text='Automatic · public extraction for this provider.';
  $('connectionHint').textContent=text;
}
function invalidateAnalysis() {
  if(activeJobId)return;
  currentUrl='';analyzedAccount=null;analysisId=null;
  $('result').dataset.ready='0';$('result').classList.add('hidden');
}
window.refreshAccountControls=async()=>{
  const response=await window.api('/api/accounts');savedYouTube=response.accounts[0];
  const configured=savedYouTube.configured;
  $('accountState').textContent=!configured?'YouTube · no connection saved.':savedYouTube.source==='browser'?`Chrome profile ${savedYouTube.profile} · configured and saved.`:'YouTube session · saved on this server.';
  const states={unknown:'Provider sign-in has not been confirmed yet. Verify one authorized members-only video; channel verification pending is not a broken connection.',
    usable:'Provider session usable · confirmed by authenticated access.',expired:'Provider session expired · sign into the saved account, then recheck the connection.',
    rejected:'Provider session rejected or signed out · sign into the saved account, then recheck.',blocked:'macOS blocked session access · allow Keychain access, then recheck.',
    unavailable:'This saved Chrome connection is local to a Mac. Connect an authorized session on this server.',missing:'Provider session missing · reconnection required.'};
  $('sessionState').textContent=configured?(states[savedYouTube.session_state] || 'Provider session needs a check.'):'';
  if(configured && savedYouTube.last_error)$('sessionState').textContent+=' '+savedYouTube.last_error;
  const checks=savedYouTube.membership_checks || {};
  const ids=[...new Set([...Object.keys(checks),...(savedYouTube.verified_channels || [])])];
  $('membershipState').textContent=!configured?'':ids.length?'Membership last verified for '+ids.map(id=>`${checks[id]?.channel || id} (${checks[id]?.evidence==='analysis'?'analysis confirmed':'download confirmed'})`).join(', ')+'.':'Membership verification pending · access is checked separately for each channel.';
  $('editChromeConnection').classList.toggle('hidden',!response.local_chrome_available);
  $('editChromeConnection').textContent=configured?'Change or recheck connection':'Connect YouTube once';
  $('disconnectAccount').classList.toggle('hidden',!configured);
  updateConnectionHint();return response;
};
$('url').addEventListener('input',()=>{if(currentUrl && $('url').value.trim()!==currentUrl)invalidateAnalysis();updateConnectionHint();});
$('analysisAccount').addEventListener('change',()=>{
  invalidateAnalysis();updateConnectionHint();
  const mode=$('analysisAccount').value;
  accountModeSaving=window.api('/api/settings',{method:'PUT',body:JSON.stringify({account_mode:mode})});
  accountModeSaving.then(()=>setStatus('Account preference saved. Analyze the link with this mode.')).catch(error=>setStatus(error.message,true));
});
$('editChromeConnection').addEventListener('click',()=>perform('settingsMessage',async()=>{
  const response=await window.api('/api/accounts/youtube/profiles');
  $('chromeProfile').replaceChildren(...response.profiles.map(profile=>{const option=document.createElement('option');option.value=profile;option.textContent=profile;return option;}));
  if(savedYouTube?.profile)$('chromeProfile').value=savedYouTube.profile;
  $('localBrowserConnection').classList.remove('hidden');
  $('connectionSetupMessage').textContent='Only the profile you select and the one test video will be read.';
}));
$('cancelChromeSetup').addEventListener('click',()=>{$('localBrowserConnection').classList.add('hidden');$('connectionTestUrl').value='';});
$('connectChrome').addEventListener('click',()=>perform('connectionSetupMessage',async()=>{
  const profile=$('chromeProfile').value,url=$('connectionTestUrl').value.trim();
  if(!profile || !youtubeLink(url))throw new Error('Select your Chrome profile and provide one HTTPS YouTube members-only video you can already watch.');
  $('connectChrome').disabled=true;
  try {
    $('connectionSetupMessage').textContent='Checking access to that one video…';
    const grant=await authorizeAccountChange('youtube-replace');
    const result=await window.api('/api/accounts/youtube/browser/verify',{method:'POST',headers:{'X-OVD-Reauth':grant},body:JSON.stringify({profile,url})});
    $('localBrowserConnection').classList.add('hidden');$('connectionTestUrl').value='';
    // Preserve an explicit Public preference; connecting an account does not change it.
    invalidateAnalysis();await window.refreshAccountControls();$('settingsMessage').textContent=result.message;
  } finally {$('connectChrome').disabled=false;}
}));
$('connectAccount').addEventListener('click',()=>perform('settingsMessage',async()=>{
  const file=$('cookieFile').files[0];if(!file || file.size>200000)throw new Error('Choose a YouTube-only session file under 200 KB.');
  if(!$('cookieConsent').checked)throw new Error('Confirm session ownership and authorized access for this import.');
  const test_url=$('importTestUrl').value.trim();if(!youtubeLink(test_url))throw new Error('Provide one authorized HTTPS YouTube members-only test video.');
  try {const grant=await authorizeAccountChange('youtube-replace');const cookies=await file.text();await window.api('/api/accounts/youtube',{method:'POST',headers:{'X-OVD-Reauth':grant},body:JSON.stringify({cookies,authorized:true,test_url})});}
  finally {$('cookieFile').value='';$('cookieConsent').checked=false;}
  $('advancedAccountSetup').open=false;invalidateAnalysis();await window.refreshAccountControls();
  $('importTestUrl').value='';$('settingsMessage').textContent='Session verified and encrypted on this server. Automatic mode can reuse it.';
}));
$('disconnectAccount').addEventListener('click',()=>perform('settingsMessage',async()=>{
  const grant=await authorizeAccountChange('youtube-disconnect');
  await window.api('/api/accounts/youtube',{method:'DELETE',headers:{'X-OVD-Reauth':grant}});invalidateAnalysis();await window.refreshAccountControls();
  $('localBrowserConnection').classList.add('hidden');$('settingsMessage').textContent='App connection removed. Chrome remains signed in.';
}));

let reauthPending=null;
async function authorizeAccountChange(action) {
  if(reauthPending)throw new Error('Complete the current account authorization first.');
  const auth=await window.api('/api/auth/status');
  $('reauthCodeLabel').classList.toggle('hidden',!auth.mfa_enabled);$('reauthCode').required=auth.mfa_enabled;
  $('reauthPassword').value='';$('reauthCode').value='';$('reauthMessage').textContent='';
  return new Promise((resolve,reject)=>{reauthPending={action,resolve,reject};$('reauthDialog').showModal();});
}
function closeReauthentication(error=null,grant=null) {
  const pending=reauthPending;reauthPending=null;
  $('reauthPassword').value='';$('reauthCode').value='';$('reauthDialog').close();
  if(pending){if(error)pending.reject(error);else pending.resolve(grant);}
}
$('cancelReauth').addEventListener('click',()=>closeReauthentication(new Error('Account change cancelled.')));
$('reauthDialog').addEventListener('cancel',event=>{event.preventDefault();closeReauthentication(new Error('Account change cancelled.'));});
$('reauthForm').addEventListener('submit',async event=>{
  event.preventDefault();if(!reauthPending)return;
  const password=$('reauthPassword').value,code=$('reauthCode').value,action=reauthPending.action;
  $('reauthPassword').value='';$('reauthCode').value='';
  const button=$('reauthForm').querySelector('button[type="submit"]');button.disabled=true;
  try {const result=await window.api('/api/auth/reauth',{method:'POST',body:JSON.stringify({password,code,action})});closeReauthentication(null,result.grant);}
  catch(error){$('reauthMessage').textContent=error.message;}finally{button.disabled=false;}
});
