const fs = require('node:fs');
const path = require('node:path');
const net = require('node:net');
const http = require('node:http');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {chromium} = require('playwright');
const root = __dirname;
const python = process.env.PYTHON || (process.platform === 'win32' ? 'python' : 'python3');
const delay = ms => new Promise(resolve => setTimeout(resolve,ms));
async function port(){const server=net.createServer();await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));const value=server.address().port;await new Promise(resolve=>server.close(resolve));return value;}
async function waitFor(check,label){for(let i=0;i<50;i++){if(await check())return;await delay(200);}throw new Error(label);}
async function sourceProxy(address,ports){
  const server=http.createServer((incoming,outgoing)=>{
    let target;try{target=new URL(incoming.url);}catch{outgoing.writeHead(400).end();return;}
    if(target.hostname!=='127.0.0.1'||!ports.includes(Number(target.port))){outgoing.writeHead(403).end();return;}
    const request=http.request(target,{method:incoming.method,headers:incoming.headers,localAddress:address},response=>{
      outgoing.writeHead(response.statusCode,response.headers);response.pipe(outgoing);
    });request.on('error',()=>outgoing.writeHead(502).end());incoming.pipe(request);
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  return {server,url:'http://127.0.0.1:'+server.address().port,address};
}
function wire(context,proxy){
  context.sourceAddress=proxy.address;
  async function request(method,url,options={}){
    const cookies=await context.cookies(url);
    const raw=options.raw!==undefined;
    const body=raw?options.raw:options.data===undefined?undefined:JSON.stringify(options.data);
    const headers={Cookie:cookies.map(item=>item.name+'='+item.value).join(';'),...options.headers};
    if(body!==undefined){headers['Content-Type']=raw?'application/octet-stream':'application/json';headers['Content-Length']=Buffer.byteLength(body);}
    return new Promise((resolve,reject)=>{
      const request=http.request(url,{method,headers,localAddress:proxy.address},response=>{
        const chunks=[];response.on('data',chunk=>chunks.push(chunk));response.on('end',()=>{
          const buffer=Buffer.concat(chunks);resolve({json:async()=>JSON.parse(buffer.toString('utf8')),body:async()=>buffer,headers:()=>response.headers,ok:()=>response.statusCode>=200&&response.statusCode<300,status:()=>response.statusCode});
        });
      });request.on('error',reject);request.end(body);
    });
  }
  context.network={get:url=>request('GET',url),post:(url,options)=>request('POST',url,options)};
}
(async()=>{
  const listen=await port(), local=await port();
  const host=process.env.MESSENGER_QA_HOST||'127.0.0.1';
  const subnet=process.env.MESSENGER_QA_NETWORK||'127.0.0.0/8';
  const url='http://'+host+':'+listen+'/';
  const control='http://127.0.0.1:'+local+'/';
  const directory=fs.mkdtempSync(path.join(root,'qa-messenger-'));
  let server, browser;const proxies=[];
  function launch(){server=spawn(python,[path.join(root,'lan_messenger.py'),'--host',host,'--network',subnet,'--port',String(listen),'--local-port',String(local),'--data-dir',directory,'--lifetime','300','--migration-target','127.0.0.3'],{windowsHide:true,stdio:['ignore','ignore','pipe']});server.stderr.on('data',chunk=>process.stderr.write(chunk));}
  async function start(){launch();await waitFor(async()=>{try{return (await fetch(url+'health')).ok;}catch{return false;}},'Helper did not start');}
  async function stop(){const admin=await(await fetch(control+'api/admin')).json();await fetch(control+'api/admin/stop',{method:'POST',headers:{'X-CSRF-Token':admin.token},body:'{}'});await new Promise(resolve=>{if(server.exitCode!==null)resolve();else server.once('exit',resolve);});}
  async function state(context){return (await context.network.get((context.base||url)+'api/state')).json();}
  async function create(context,name){const page=await context.newPage();await page.goto(context.base||url);await page.waitForFunction(()=>document.getElementById('name').value.length>0);assert.equal(await page.locator('#your-ip').textContent(),context.sourceAddress);await page.getByLabel('Computer name',{exact:true}).fill(name);await page.locator('#profile').getByRole('button',{name:'Save',exact:true}).click();await waitFor(async()=>(await state(context)).me.name===name,'Name not saved');return page;}
  async function choose(page,id){await page.locator('[data-device="'+id+'"]').click();await page.locator('#compose').waitFor({state:'visible'});}
  async function send(page,text){await page.getByLabel('Message',{exact:true}).fill(text);await page.getByRole('button',{name:'Send',exact:true}).click();await waitFor(async()=>await page.locator('#compose').inputValue()==='','Browser message did not send');assert.equal(await page.locator('#error').textContent(),'');}
  try{
    await start();
    for(const address of ['127.0.0.2','127.0.0.3','127.0.0.4'])proxies.push(await sourceProxy(address,[listen,local]));
    const proxyOptions=proxy=>({server:proxy.url,bypass:'<-loopback>'});
    browser=await chromium.launch({channel:process.env.MESSENGER_BROWSER_CHANNEL || undefined,headless:true,proxy:proxyOptions(proxies[0]),args:['--proxy-bypass-list=<-loopback>']});
    const a=await browser.newContext({viewport:{width:1280,height:800},proxy:proxyOptions(proxies[0])});wire(a,proxies[0]);
    a.base=control;
    const b=await browser.newContext({viewport:{width:1280,height:800},proxy:proxyOptions(proxies[1])});wire(b,proxies[1]);
    const c=await browser.newContext({proxy:proxyOptions(proxies[2])});wire(c,proxies[2]);
    const windows=await create(a,'Windows laptop');
    let ubuntu=await create(b,'Ubuntu desktop');
    const spare=await create(c,'Spare computer');
    const alice=(await state(a)).me.id, bob=(await state(b)).me.id;
    const clone=await browser.newContext({proxy:proxyOptions(proxies[0])});wire(clone,proxies[0]);clone.base=control;
    const clonePage=await create(clone,'Windows laptop');
    assert.equal((await state(clone)).me.id,alice);
    assert.equal((await state(a)).devices.length,2);
    await clonePage.close();
    assert((await state(b)).devices.find(item=>item.id===alice).online);
    await windows.getByRole('button',{name:'Find devices & invite',exact:true}).click();
    await windows.locator('#neighbors').filter({hasText:/Open the invite link to join|Joined|No additional addresses/}).waitFor();
    assert.equal(await windows.locator('#dialog-url').textContent(),url);
    await windows.getByRole('button',{name:'Close',exact:true}).click();
    await waitFor(async()=>await windows.locator('[data-device="'+bob+'"]').count()===1,'Joined device not discovered');
    await choose(windows,bob);
    await send(windows,'Hello Ubuntu. This conversation is saved.');
    await waitFor(async()=>(await state(b)).unread===1,'Unread inbox did not update');
    await waitFor(async()=>await ubuntu.locator('[data-device="'+alice+'"]').count()===1,'Windows contact missing');
    await choose(ubuntu,alice);
    await windows.locator('#compose').fill('Keep this unsent Windows draft');
    await ubuntu.bringToFront();
    const reply='Ubuntu reply\nسلام\n<script>window.unwanted=true</script>';
    await send(ubuntu,reply);
    await windows.waitForFunction(expected=>Array.from(document.querySelectorAll('.body')).some(item=>item.textContent===expected),reply);
    assert.equal(await windows.locator('#compose').inputValue(),'Keep this unsent Windows draft');
    assert.equal(await windows.locator('textarea').count(),1);
    assert.equal(await windows.evaluate(()=>Boolean(window.unwanted)),false);
    assert.equal((await(await c.network.get(url+'api/history?peer='+alice)).json()).messages.length,0);
    assert.equal((await(await c.network.get(url+'api/history?peer='+bob)).json()).messages.length,0);
    const privateState=await state(c);
    assert(privateState.devices.every(item=>item.preview===''));
    assert.equal(privateState.migration.command,undefined);
    console.log('PASS: three devices, browser sends both ways, unread inboxes, isolated histories, plain-text rendering, and draft preservation.');
    await ubuntu.close();
    await send(windows,'This message waited while Ubuntu was closed.');
    ubuntu=await b.newPage();await ubuntu.goto(url);await waitFor(async()=>await ubuntu.locator('[data-device="'+alice+'"]').count()===1,'Reopened identity missing');
    assert.equal((await state(b)).me.id,bob);
    await choose(ubuntu,alice);
    await ubuntu.waitForFunction(()=>Array.from(document.querySelectorAll('.body')).some(item=>item.textContent==='This message waited while Ubuntu was closed.'));
    const payload=Buffer.from(Array.from({length:1024*1024+37},(_,index)=>index%256));
    const filename='نمونه "test" <script>.bin';
    await windows.locator('#compose').fill('Attached binary and empty files.');
    await windows.locator('#file-input').setInputFiles([
      {name:filename,mimeType:'application/octet-stream',buffer:payload},
      {name:'empty.txt',mimeType:'text/plain',buffer:Buffer.alloc(0)},
      {name:'remove-before-send.txt',mimeType:'text/plain',buffer:Buffer.from('Do not send')},
    ]);
    assert.equal(await windows.locator('.pending-file').count(),3);
    await windows.getByRole('button',{name:'Remove remove-before-send.txt',exact:true}).click();
    assert.equal(await windows.locator('.pending-file').count(),2);
    await ubuntu.close();
    await windows.getByRole('button',{name:'Send',exact:true}).click();
    await waitFor(async()=>await windows.locator('.pending-file').count()===0&&!await windows.locator('#send').isDisabled(),'File upload did not finish');
    assert.equal(await windows.locator('#error').textContent(),'');
    const files=(await(await a.network.get(a.base+'api/history?peer='+bob)).json()).messages.filter(item=>item.attachment);
    assert.equal(files.length,2);assert.equal(files[0].attachment.name,filename);assert.equal(files[0].attachment.size,payload.length);assert.equal(files[1].attachment.size,0);
    assert((await state(b)).devices.find(item=>item.id===alice).preview.includes('empty.txt'));
    ubuntu=await b.newPage();await ubuntu.goto(url);await waitFor(async()=>await ubuntu.locator('[data-device="'+alice+'"]').count()===1,'File conversation missing');await choose(ubuntu,alice);
    await ubuntu.getByRole('link',{name:'Download '+filename,exact:true}).waitFor();
    const downloadEvent=ubuntu.waitForEvent('download');await ubuntu.getByRole('link',{name:'Download '+filename,exact:true}).click();
    const download=await downloadEvent;assert.equal(download.suggestedFilename(),filename.replace(/[<>:"/\\|?*]/g,'_'));
    const downloadPath=path.join(directory,'received.bin');await download.saveAs(downloadPath);
    assert.equal(crypto.createHash('sha256').update(fs.readFileSync(downloadPath)).digest('hex'),crypto.createHash('sha256').update(payload).digest('hex'));
    const emptyDownload=await b.network.get(url+'api/file?id='+files[1].id);assert.equal((await emptyDownload.body()).length,0);
    const forbidden=await c.network.get(url+'api/file?id='+files[0].id);assert.equal(forbidden.status(),404);
    const invalidId=await a.network.get(a.base+'api/file?id=../../uploads');assert.equal(invalidId.status(),400);
    const deniedUpload=await a.network.post(a.base+'api/upload?peer='+bob+'&name=bad.txt',{headers:{'X-CSRF-Token':'wrong'},raw:Buffer.from('not saved')});assert.equal(deniedUpload.status(),403);
    const originUpload=await a.network.post(a.base+'api/upload?peer='+bob+'&name=bad.txt',{headers:{'X-CSRF-Token':(await state(a)).csrf,'Origin':'http://unrelated.invalid'},raw:Buffer.from('not saved')});assert.equal(originUpload.status(),403);
    const missingPeer=await a.network.post(a.base+'api/upload?peer=missing&name=bad.txt',{headers:{'X-CSRF-Token':(await state(a)).csrf},raw:Buffer.from('not saved')});assert.equal(missingPeer.status(),400);
    const emptyName=await a.network.post(a.base+'api/upload?peer='+bob+'&name=',{headers:{'X-CSRF-Token':(await state(a)).csrf},raw:Buffer.from('not saved')});assert.equal(emptyName.status(),400);
    console.log('PASS: multiple browser file uploads, optional text, selection removal, offline delivery, Unicode names, empty files, byte-identical browser download, and unrelated-device access denied.');
    let current=await state(a);
    for(let i=0;i<105;i++){const response=await a.network.post(a.base+'api/send',{headers:{'X-CSRF-Token':current.csrf},data:{peer:bob,text:'Saved history message '+i}});assert(response.ok());}
    await ubuntu.reload();await waitFor(async()=>await ubuntu.locator('[data-device="'+alice+'"]').count()===1,'History contact missing');await choose(ubuntu,alice);
    await ubuntu.getByRole('button',{name:'Load earlier messages',exact:true}).waitFor({state:'visible'});
    await ubuntu.getByRole('button',{name:'Load earlier messages',exact:true}).click();
    await ubuntu.waitForFunction(()=>document.querySelectorAll('.message').length>100);
    console.log('PASS: offline delivery, remembered device identity, and paginated conversation history.');
    const prior=(await(await a.network.get(a.base+'api/history?peer='+bob)).json()).messages;
    await stop();await start();
    await windows.reload();await waitFor(async()=>(await state(a)).me.id===alice,'Identity lost after restart');
    const restored=(await(await a.network.get(a.base+'api/history?peer='+bob)).json()).messages;
    assert.equal(restored.length,prior.length);
    assert.equal(restored[restored.length-1].body,prior[prior.length-1].body);
    const fileAfterRestart=await b.network.get(url+'api/file?id='+files[0].id);assert(fileAfterRestart.ok());assert((await fileAfterRestart.body()).equals(payload));
    assert(fileAfterRestart.headers()['content-disposition'].startsWith('attachment;'));
    assert.equal(fileAfterRestart.headers()['content-type'],'application/octet-stream');
    assert.equal(fileAfterRestart.headers()['x-content-type-options'],'nosniff');
    await choose(windows,bob);
    await windows.bringToFront();
    await windows.screenshot({path:path.join(root,'lan-messenger-preview.png'),fullPage:true});
    const mobile=await a.newPage();await mobile.setViewportSize({width:390,height:844});await mobile.goto(a.base);await mobile.waitForFunction(()=>document.getElementById('name').value.length>0);
    assert.equal(await mobile.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    await waitFor(async()=>await mobile.locator('[data-device="'+bob+'"]').count()===1,'Mobile contact missing');await choose(mobile,bob);
    await mobile.locator('#file-input').setInputFiles({name:'mobile-'.repeat(40)+'.txt',mimeType:'text/plain',buffer:Buffer.from('Mobile file')});
    assert.equal(await mobile.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    await mobile.getByRole('button',{name:'Send',exact:true}).click();await waitFor(async()=>await mobile.locator('.pending-file').count()===0&&!await mobile.locator('#send').isDisabled(),'Mobile file failed');
    assert.equal(await mobile.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    const bad=await a.network.post(a.base+'api/send',{headers:{'X-CSRF-Token':'wrong'},data:{peer:bob,text:'should fail'}});assert.equal(bad.status(),403);
    const wrongOrigin=await a.network.post(a.base+'api/send',{headers:{'X-CSRF-Token':(await state(a)).csrf,'Origin':'http://unrelated.invalid'},data:{peer:bob,text:'should fail'}});assert.equal(wrongOrigin.status(),403);
    console.log('PASS: history and identity survive restart; mobile layout fits; invalid tokens and origins are rejected.');
    const migration=await(await fetch(control+'api/admin/migration')).json();
    assert(migration.enabled&&!migration.paused);
    const setupURL=migration.command.match(/open\('([^']+)'\)/)[1];
    assert.equal((await c.network.get(setupURL)).status(),403);
    const installer=await b.network.get(setupURL);assert(installer.ok());
    assert((await installer.body()).toString('utf8').includes('systemd'));
    assert.equal((await b.network.get(url+'api/admin/migration')).status(),404);
    const exported=await b.network.get(setupURL.replace('/install.py','/bundle.tar'));assert(exported.ok());
    const tar=await exported.body();assert(tar.includes(Buffer.from('lan-messenger/migration.json')));
    assert((await state(a)).migration.paused);
    await waitFor(async()=>await windows.locator('#send').isDisabled(),'Source did not pause during migration');
    const paused=await a.network.post(a.base+'api/send',{headers:{'X-CSRF-Token':(await state(a)).csrf},data:{peer:bob,text:'Must not be lost after transfer'}});assert.equal(paused.status(),400);
    const admin=await(await fetch(control+'api/admin')).json();
    assert.equal((await b.network.post(url+'api/admin/migration/resume',{headers:{'X-CSRF-Token':admin.token},data:{}})).status(),403);
    await windows.getByRole('button',{name:'Cancel move',exact:true}).click();
    await waitFor(async()=>!(await state(a)).migration.paused,'Local cancel did not resume the source');
    const exportedAgain=await b.network.get(setupURL.replace('/install.py','/bundle.tar'));assert(exportedAgain.ok());
    assert.equal((await b.network.get(setupURL.replace('/install.py','/complete'))).status(),200);
    await waitFor(async()=>await windows.locator('#open-ubuntu').isVisible(),'Completed migration did not show the Ubuntu link');
    assert.equal(await windows.locator('#open-ubuntu').getAttribute('href'),'http://127.0.0.3:8765/');
    console.log('PASS: private Ubuntu-only setup and streamed export; source pauses, owner can cancel, and completed handover shows the new host.');
    await stop();
  }finally{
    if(browser)await browser.close();
    for(const proxy of proxies){proxy.server.closeAllConnections();await new Promise(resolve=>proxy.server.close(resolve));}
    if(server&&server.exitCode===null)server.kill();
  }
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
