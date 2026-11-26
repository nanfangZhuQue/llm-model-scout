window.createProviderUI = ({api, notice, showError, protocolLabel, getProvider, onSaved, onTask, onCatalogChange}) => {
  const $ = id => document.getElementById(id);
  const kinds = {openai: 'openai_chat', anthropic: 'anthropic_messages', responses: 'openai_responses'};
  let editing = null, exporting = null, enabled = false, busy = false, catalog = null;
  let epoch = 0, exportSerial = 0;
  const selected = new Set();
  const element = (tag, text) => {const e = document.createElement(tag); if(text !== undefined)e.textContent=String(text);return e;};
  const key = row => `${row.endpoint_id}\u0000${row.model}`;
  function toggleFields() {
    for(const name of Object.keys(kinds)) {
      const active=$("enable-"+name).checked;
      $("fields-"+name).hidden=!active;$("fields-"+name).disabled=!active;
      $("url-"+name).required=active;
      const old=editing?.connection?.endpoints.find(e=>e.protocol===kinds[name]);
      $("key-"+name).required=active&&!old?.has_key;
    }
  }
  function clearEditor() {
    $("provider-dialog").close();$("provider-form").reset();editing=null;
    $("provider-form-error").textContent='';toggleFields();
  }
  function edit(record) {
    if(!enabled||busy)return;
    clearEditor();editing=record;
    $("provider-title").textContent=record?'编辑服务商':'新增服务商';
    $("provider-name").value=record?.name||'';
    $("delete-provider").hidden=!record;
    for(const name of Object.keys(kinds)) {
      const old=record?.connection?.endpoints.find(e=>e.protocol===kinds[name]);
      $("enable-"+name).checked=record?Boolean(old):name==='openai';
      $("url-"+name).value=old?.base_url||'';
      $("key-"+name).value='';$("key-"+name).placeholder=old?'已保存，留空保持不变':'';
      $("proxy-"+name).value='';
    }
    toggleFields();$("provider-dialog").showModal();
  }
  for(const name of Object.keys(kinds))$("enable-"+name).addEventListener('change',toggleFields);
  $("new-provider").addEventListener('click',()=>edit(null));
  $("edit-provider").addEventListener('click',()=>{const p=getProvider();if(p?.editable)edit(p);});
  $("close-provider").addEventListener('click',()=>{if(!busy)clearEditor();});
  $("provider-dialog").addEventListener('cancel',event=>{if(busy){event.preventDefault();return;}$("provider-form").reset();editing=null;});
  $("provider-form").addEventListener('submit',async event=>{
    event.preventDefault();if(busy)return;
    const payload={name:$("provider-name").value.trim(),endpoints:[]};
    for(const name of Object.keys(kinds))if($("enable-"+name).checked) {
      const endpoint={protocol:kinds[name],base_url:$("url-"+name).value.trim()};
      if($("key-"+name).value)endpoint.api_key=$("key-"+name).value.trim();
      const proxy=$("proxy-"+name).value.trim();
      if(proxy)endpoint.proxy=proxy;
      else if(!editing||$("clear-proxy-"+name).checked)endpoint.proxy=null;
      payload.endpoints.push(endpoint);
    }
    if(!payload.endpoints.length){$("provider-form-error").textContent='至少选择一种协议。';return;}
    if(editing)payload.revision=editing.revision;
    const generation=epoch;
    busy=true;$("save-provider").disabled=true;
    try {
      const saved=await api(editing?`/providers/${encodeURIComponent(editing.provider_id)}`:'/providers',
        {method:editing?'PUT':'POST',body:payload,tag:'provider'});
      if(generation!==epoch)return;
      clearEditor();catalog=null;selected.clear();$("catalog-panel").hidden=true;
      await onSaved(saved.provider_id);notice('服务商已加密保存到本机，下次启动自动恢复。');
    } catch(error) {if(generation===epoch)$("provider-form-error").textContent=error.status===503?'本机安全存储不可用，未降级为明文保存。':error.message;}
    finally{if(generation===epoch){busy=false;$("save-provider").disabled=false;}}
  });
  $("delete-provider").addEventListener('click',async()=>{
    if(!editing||!confirm('删除本机保存的服务商？已有报告保留，运行中的任务需先完成。'))return;
    try{await api(`/providers/${encodeURIComponent(editing.provider_id)}?revision=${editing.revision}`,{method:'DELETE',tag:'provider'});clearEditor();catalog=null;$("catalog-panel").hidden=true;await onSaved(null);notice('已删除本机服务商记录。');}catch(error){$("provider-form-error").textContent=error.message;}
  });
  $("export-provider").addEventListener('click',()=>{exporting=getProvider();if(!exporting?.editable)return;exportSerial++;$("confirm-export").disabled=false;$("export-secrets").checked=false;$("export-dialog").showModal();});
  $("close-export").addEventListener('click',()=>{exportSerial++;$("export-dialog").close();});
  $("export-dialog").addEventListener('cancel',()=>{exportSerial++;});
  $("confirm-export").addEventListener('click',async()=>{
    if(!exporting)return;
    const include=$("export-secrets").checked, target=exporting, serial=exportSerial, generation=epoch;
    $("confirm-export").disabled=true;
    try {
      const blob=await api(`/providers/${encodeURIComponent(target.provider_id)}/export`,
        {method:'POST',body:{include_secrets:include,confirm_plaintext:include},blob:true,tag:'download'});
      if(serial!==exportSerial||generation!==epoch)return;
      const url=URL.createObjectURL(blob),a=element('a');a.href=url;
      a.download=`provider-${target.provider_id}${include?'.private':''}.json`;
      document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
      $("export-dialog").close();notice(include?'已导出含明文密钥的配置，请勿上传云仓库。':'已导出脱敏配置。');
    } catch(error){if(serial===exportSerial&&generation===epoch)showError(error);}
    finally{if(serial===exportSerial&&generation===epoch)$("confirm-export").disabled=false;}
  });
  $("discover-models").addEventListener('click',async()=>{
    const p=getProvider();if(!p||busy)return;
    busy=true;$("discover-models").disabled=true;
    try{await onTask({provider_id:p.provider_id,endpoint_id:$("endpoint").value,operation:'models'});}
    catch(error){showError(error);}finally{busy=false;$("discover-models").disabled=!getProvider();}
  });
  function matches(){
    const q=$("catalog-search").value.toLowerCase(), protocol=$("catalog-protocol").value;
    return (catalog?.rows||[]).filter(row=>(!protocol||row.protocol===protocol)&&row.model.toLowerCase().includes(q));
  }
  function batchRequests(){
    const provider=getProvider();
    if(!catalog||provider?.provider_id!==catalog.provider_id)return null;
    const all={provider_id:provider.provider_id,endpoint_id:'all',mode:'batch'};
    const mapping=Object.create(null);
    for(const row of catalog.rows)if(selected.has(key(row)))(mapping[row.endpoint_id]??=[]).push(row.model);
    return {all,selected:selected.size?{...all,model_selections:mapping}:null};
  }
  function catalogSelectionChanged(){
    $("catalog-count").textContent=`共 ${catalog.rows.length} 条候选，已选 ${selected.size} 条`;
    onCatalogChange();
  }
  function renderCatalog(){
    if(!catalog)return;
    const rows=matches();
    $("catalog-list").replaceChildren(...rows.slice(0,1000).map(row=>{
      const label=element('label'),checkbox=element('input');label.className='catalog-row';checkbox.type='checkbox';checkbox.checked=selected.has(key(row));
      checkbox.addEventListener('change',()=>{if(checkbox.checked)selected.add(key(row));else selected.delete(key(row));catalogSelectionChanged();});
      label.append(checkbox,element('span',`${row.endpoint_id} · ${protocolLabel(row.protocol)} / ${row.model}`));return label;
    }));
    if(rows.length>1000)$("catalog-list").append(element('p','仅显示前1000条，请搜索定位；全选覆盖全部匹配项。'));
    catalogSelectionChanged();
  }
  $("catalog-search").addEventListener('input',renderCatalog);
  $("catalog-protocol").addEventListener('change',renderCatalog);
  $("catalog-all").addEventListener('click',()=>{for(const row of matches())selected.add(key(row));renderCatalog();});
  $("catalog-none").addEventListener('click',()=>{selected.clear();renderCatalog();});
  for(const [id,slot] of [['catalog-test','selected'],['catalog-test-all','all']]){
    $(id).addEventListener('click',async()=>{
      const payload=batchRequests()?.[slot];if(!payload)return;
      try{await onTask(payload,slot);}catch(error){showError(error);}
    });
  }
  return {
    batchRequests,
    update(management){enabled=management;$("provider-actions").hidden=!management;const p=getProvider();$("edit-provider").disabled=$("export-provider").disabled=!p?.editable;$("discover-models").disabled=!p||busy;},
    clear(){epoch++;exportSerial++;busy=false;$("save-provider").disabled=false;clearEditor();$("export-dialog").close();exporting=null;catalog=null;selected.clear();$("catalog-panel").hidden=true;},
    catalogue(report,job){
      if(job.operation!=='models')return;
      catalog={provider_id:job.provider_id,rows:report.discovery.flatMap(d=>d.models.map(model=>({endpoint_id:d.endpoint_id,protocol:d.protocol,model})))};
      selected.clear();$("catalog-search").value='';$("catalog-panel").hidden=false;
      const protocols=[...new Set(catalog.rows.map(row=>row.protocol))];
      $("catalog-protocol").replaceChildren(new Option('全部协议',''),...protocols.map(protocol=>new Option(protocolLabel(protocol),protocol)));
      $("catalog-protocol-field").hidden=protocols.length<2;
      $("catalog-status").textContent=report.discovery.map(d=>`${d.endpoint_id} · ${protocolLabel(d.protocol)}: ${d.status}${d.error_message?' — '+d.error_message:''}`).join('；')+'。候选列表不是可用性证明。';
      renderCatalog();
    }
  };
};
