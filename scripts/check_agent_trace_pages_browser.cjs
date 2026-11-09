// Real HTTP + actual MySQL. Writes an explicitly named verification upload batch.
const fs=require('node:fs'),path=require('node:path'),{createRequire}=require('node:module');
const root=path.resolve(__dirname,'../storage/browser-check');
const runtime=createRequire(path.join(process.env.CODEX_BROWSER_MODULES||'C:/Users/杨/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules','_check.cjs'));
const {chromium}=runtime('playwright');
(async()=>{
 const session=JSON.parse(fs.readFileSync(path.join(root,'session.json'),'utf8'));
 const prior=JSON.parse(fs.readFileSync(path.join(root,'mysql-http-report.json'),'utf8'));
 const browser=await chromium.launch({channel:'msedge',headless:true});
 const context=await browser.newContext({viewport:{width:1900,height:1400},acceptDownloads:true});
 const p=await context.newPage(),errors=[],requests=[],checks=[];
 p.on('pageerror',e=>errors.push(e.message));
 p.on('response',r=>{if(r.url().includes('/api/v1/')){const u=new URL(r.url());requests.push({path:u.pathname,status:r.status(),view:u.searchParams.get('view')});}});
 await p.addInitScript(s=>{sessionStorage.setItem('accessToken',s.token);sessionStorage.setItem('authUser',JSON.stringify(s.user));},session);
 async function visit(view,batch){
  await p.goto(`http://127.0.0.1:1225/compliance/agent-traces?view=${view}&batchId=${batch}`,{waitUntil:'networkidle',timeout:90000});
  const toggle=p.locator('.compliance-mode .el-switch');
  if(await toggle.evaluate(e=>e.classList.contains('is-checked')))await toggle.click();
  // These views initialize from the catalog, not the route's batchId. Select through
  // their existing controls so verification remains valid after adding upload batches.
  if(batch && ['statistics','execution'].includes(view)){
   const display='AUD-'+batch.slice(-10).toUpperCase();
   const input=p.locator(view==='statistics'?'#ats-batch':'.ex-query .el-select input').first();
   await input.fill(display);
   await p.getByRole('option',{name:new RegExp('^'+display)}).click();
   const loaded=p.waitForResponse(r=>r.url().includes(view==='statistics'?'/agent-traces/statistics?':'/agent-traces/workspace?') && new URL(r.url()).searchParams.get('batch_id')===batch);
   await p.locator(view==='statistics'?'.ats-filters':'.ex-query').getByRole('button',{name:'查询',exact:true}).click();
   await loaded;
  }
 }
 async function download(kind,filename){
  const [file]=await Promise.all([p.waitForEvent('download',{timeout:120000}),p.getByRole('button',{name:kind,exact:true}).click()]);
  await file.saveAs(path.join(root,filename));
 }
 try{
  await visit('statistics',prior.http.batch_id);
  await p.locator('.ats-kpis').waitFor({timeout:60000});
  if(await p.locator('.ats-kpis strong').count()!==8)throw Error('Missing KPI fields');
  await p.locator('.ats-statistics canvas').first().waitFor();
  await p.screenshot({path:path.join(root,'statistics-value-results.png'),fullPage:true});
  const kpis=await p.locator('.ats-kpis').innerText();
  const risk=p.waitForResponse(r=>r.url().includes('list_filter=risk')&&r.url().includes('/samples?'));
  await p.locator('.ats-list-tabs').getByRole('button',{name:/^检出风险/}).click();await risk;
  if(await p.locator('.ats-kpis').innerText()!==kpis)throw Error('List filter changed batch KPIs');
  const all=p.waitForResponse(r=>r.url().includes('list_filter=all')&&r.url().includes('/samples?'));
  await p.locator('.ats-list-tabs').getByRole('button',{name:/^全部样本/}).click();await all;
  await p.getByRole('button',{name:/查看 JSON/}).nth(5).click();
  const drawer=p.locator('.ats-json-drawer');await drawer.locator('.ats-drawer-meta').waitFor();
  await drawer.getByRole('button',{name:'下一条',exact:true}).click();
  await p.waitForFunction(()=>document.querySelector('.ats-drawer-heading')?.textContent.includes('AUD-0007'));
  await p.screenshot({path:path.join(root,'statistics-json-navigation.png'),fullPage:true});
  await drawer.getByRole('button',{name:'导出 JSON',exact:true}).click();
  const jsonButton=p.locator('.ats-export-status').getByRole('button',{name:/^下载 /}).first();await jsonButton.waitFor({timeout:90000});
  await drawer.locator('.el-drawer__close-btn').click();
  const [jsonFile]=await Promise.all([p.waitForEvent('download'),jsonButton.click()]);
  await jsonFile.saveAs(path.join(root,'statistics-full-sample.json'));
  JSON.parse(fs.readFileSync(path.join(root,'statistics-full-sample.json'),'utf8'));
  checks.push('statistics_real_value_results','kpis_and_charts','risk_filter_preserves_aggregate','cross_page_json_navigation','full_sample_json_download');

  const created=await context.request.post('http://127.0.0.1:1225/api/v1/compliance/agent-traces/batches',{
   headers:{Authorization:'Bearer '+session.token,'X-Request-Id':'browser-create-'+Date.now()},data:{name:'三页联调上传批次 '+new Date().toISOString()}});
  if(!created.ok())throw Error('Batch create failed '+await created.text());
  const batch=(await created.json()).data.batch_id;
  await visit('ingest',batch);await p.locator('.at-formats').waitFor({timeout:30000});
  const openai={language:'zh',task_status:'completed',duration_ms:1000,usage:{input_tokens:100,output_tokens:60,reasoning_tokens:20},
   messages:[{role:'user',content:'联调测试样本，查询公开资料'},{role:'assistant',tool_calls:[{id:'browser-call-1',function:{name:'search',arguments:'{"User_ID":"test-a","q":"public report"}'}}]},{role:'tool',tool_call_id:'browser-call-1',content:'public result'}]};
  const second={language:'en',task_status:'completed',duration_ms:2000,usage:{input_tokens:130,output_tokens:70,reasoning_tokens:0},messages:[{role:'user',content:'Verification sample'},{role:'assistant',content:'Verification response'}]};
  const anthropic={language:'en',messages:[{role:'assistant',content:[{type:'tool_use',id:'test-tool-1',name:'fetch',input:{url:'https://example.com'}}]},{role:'user',content:[{type:'tool_result',tool_use_id:'test-tool-1',content:'public page'}]}]};
  await p.locator('input[type=file]').setInputFiles([
   {name:'verification-openai.jsonl',mimeType:'application/json',buffer:Buffer.from(JSON.stringify(openai)+'\n'+JSON.stringify(second))},
   {name:'verification-anthropic.jsonl',mimeType:'application/json',buffer:Buffer.from(JSON.stringify(anthropic))}
  ]);
  await p.waitForFunction(()=>document.querySelectorAll('.at-file').length===2);
  await p.getByRole('button',{name:'开始解析',exact:true}).click();
  await p.waitForFunction(()=>document.querySelector('.at-task')?.textContent.includes('succeeded'),null,{timeout:90000});
  await p.locator('.at-preview').waitFor({timeout:30000}).catch(()=>{});
  await p.waitForFunction(()=>document.querySelector('.at-context')?.textContent.includes('AUD-0001'));
  await p.screenshot({path:path.join(root,'ingest-real-upload.png'),fullPage:true});
  checks.push('file_upload_and_atomic_binding','real_openai_anthropic_parsing','ingest_detail_and_validation');
  await download('导出当前视图','ingest-browser-view.png');
  checks.push('ingest_png');

  await visit('statistics',batch);await p.locator('.ats-kpis').waitFor({timeout:45000});
  const sampleCount=await p.locator('.ats-kpis strong').first().innerText();if(sampleCount.trim()!=='3')throw Error('Expected 3 imported samples: '+sampleCount);
  await p.locator('.ats-list-tabs').getByRole('button',{name:/^缺失字段/}).click();
  await p.waitForFunction(()=>document.querySelectorAll('.ats-sample-table .el-table__body tbody tr').length===1);
  await p.screenshot({path:path.join(root,'statistics-imported-files.png'),fullPage:true});
  await download('导出当前视图','statistics-browser-view.png');
  await p.getByRole('button',{name:'导出结果包',exact:true}).click();
  const statsDialog=p.getByRole('dialog',{name:'导出结果包',exact:true});await statsDialog.getByRole('button',{name:'生成 ZIP',exact:true}).click();
  const zipButton=p.locator('.ats-export-status').getByRole('button',{name:/^下载 /}).first();await zipButton.waitFor({timeout:90000});
  const zipEvent=p.waitForEvent('download');await zipButton.click();const zip=await zipEvent;await zip.saveAs(path.join(root,'statistics-package.zip'));
  checks.push('missing_fields_filter','statistics_png','statistics_zip');

  await visit('execution',prior.http.batch_id);await p.locator('.ex-columns').waitFor({timeout:45000});
  await p.getByRole('button',{name:'12 导出请求',exact:true}).click();
  await p.waitForFunction(()=>document.querySelector('.execution-view tr.selected')?.textContent.includes('STEP-12'));
  await p.getByRole('button',{name:'下一步',exact:true}).click();
  await p.waitForFunction(()=>document.querySelector('.execution-view tr.selected')?.textContent.includes('STEP-13'));
  await p.screenshot({path:path.join(root,'execution-full-interface.png'),fullPage:true});
  await download('导出当前视图','execution-browser-view.png');
  checks.push('execution_graph_event_selection','execution_next_step','execution_png');
  if(errors.length)throw Error(errors.join('\n'));
  const failures=requests.filter(r=>r.status>=400);if(failures.length)throw Error(JSON.stringify(failures));
  fs.writeFileSync(path.join(root,'three-pages-browser-report.json'),JSON.stringify({mode:'real_http',database:'actual_mysql',upload_batch_id:batch,checks,requests,page_errors:errors},null,2));
  console.log(JSON.stringify({passed:true,checks,requests:requests.length,upload_batch_id:batch}));
 }catch(e){await p.screenshot({path:path.join(root,'three-pages-failure.png'),fullPage:true}).catch(()=>{});fs.writeFileSync(path.join(root,'three-pages-failure.json'),JSON.stringify({message:e.message,errors,requests},null,2));throw e;}
 finally{await browser.close();}
})().catch(e=>{console.error(e.stack);process.exitCode=1;});
