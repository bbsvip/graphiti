import assert from 'node:assert/strict';
import { createServer, type IncomingMessage } from 'node:http';
import { test } from 'node:test';
import { GraphitiMcpClient } from '../src/mcp-client.js';
import { GraphitiBackend } from '../src/backend.js';
import { registerGraphitiTool } from '../src/tool.js';
import { setupGraphitiSync } from '../src/sync.js';
import { enqueue, drain, spoolFile, spoolDir } from '../src/spool.js';
import { mkdtempSync, readFileSync, rmSync, writeFileSync, mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

async function fixture(t: any, reply: (body: any, request: IncomingMessage) => any,
  initialized: () => Promise<void> = async () => {}) {
  const calls: any[] = [];
  let handshakes = 0;
  const server = createServer(async (req, res) => {
    let raw = '';
    for await (const chunk of req) raw += chunk;
    const body = JSON.parse(raw);
    if (body.method === 'initialize') {
      handshakes++;
      res.setHeader('Mcp-Session-Id', `session-${handshakes}`);
      res.end(JSON.stringify({jsonrpc:'2.0', id:body.id, result:{serverInfo:{name:'test'}}}));
    } else if (body.method === 'notifications/initialized') {
      await initialized();
      res.writeHead(202); res.end();
    } else {
      calls.push(body);
      const output = reply(body, req);
      if (output.status) { res.writeHead(output.status); res.end(output.text); }
      else res.end(JSON.stringify({jsonrpc:'2.0', id:body.id, ...output}));
    }
  });
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise<void>(resolve => { server.close(resolve as any); server.closeAllConnections(); }));
  const address = server.address() as {port:number};
  return {url:`http://127.0.0.1:${address.port}/mcp/`, calls, handshakes:()=>handshakes};
}

for (const [label, output] of [
  ['isError', {result:{isError:true, content:[{type:'text',text:'denied'}]}}],
  ['JSON-RPC', {error:{code:-32000, message:'denied'}}],
  ['ErrorResponse text', {result:{content:[{type:'text',text:'{"error":"denied"}'}]}}],
  ['ErrorResponse wrapped', {result:{structuredContent:{result:{error:'denied'}}, content:[]}}],
  ['ErrorResponse structured', {result:{structuredContent:{error:'denied'}, content:[]}}],
  ['missing result', {}],
  ['array result', {result:[]}],
] as const) {
  test(`reject ${label} without reporting accepted`, async t => {
    const f = await fixture(t, () => output);
    await assert.rejects(new GraphitiMcpClient({url:f.url}).callTool('add_memory'), /denied|missing|Missing/);
    assert.equal(f.calls.length, 1);
  });
}

test('expired session reinitializes once after definite pre-admission 404, including writes', async t => {
  const f = await fixture(t, (_body, req) => req.headers['mcp-session-id'] === 'session-1'
    ? {status:404, text:'Session not found'}
    : {result:{content:[{type:'text',text:'{"status":"queued","uuid":"stable"}'}]}});
  const client = new GraphitiMcpClient({url:f.url});
  const result = await client.callTool('add_memory', {uuid:'stable'});
  assert.match(result.text, /queued/);
  assert.equal(f.handshakes(), 2);
  assert.equal(f.calls.length, 2);
  assert.equal(f.calls[0].params.arguments.uuid, f.calls[1].params.arguments.uuid);
});

test('expiration recovery stops after second rejection', async t => {
  const f = await fixture(t, () => ({status:404, text:'Session not found'}));
  await assert.rejects(new GraphitiMcpClient({url:f.url}).callTool('get_status'), /404|Session/);
  assert.equal(f.handshakes(), 2);
  assert.equal(f.calls.length, 2);
});

test('ambiguous write HTTP 500 is not replayed', async t => {
  const f = await fixture(t, () => ({status:500, text:'server failed after accepting'}));
  await assert.rejects(new GraphitiMcpClient({url:f.url}).callTool('add_memory'), /500/);
  assert.equal(f.calls.length, 1);
  assert.equal(f.handshakes(), 1);
});

test('backend preserves acknowledgement and sends an episode UUID', async t => {
  const f = await fixture(t, body => ({result:{content:[{type:'text',text:JSON.stringify({
    status:'queued', uuid:body.params.arguments.uuid, message:'not yet stored in Neo4j'})}]}}));
  const backend = new GraphitiBackend({url:f.url, groupId:'diagnostic', projectGroupId:null,
    projectScoping:false, timeoutMs:1000});
  const result = await backend.addEpisode({name:'small', body:'body'});
  assert.equal(typeof f.calls[0].params.arguments.uuid, 'string');
  assert.match(result.text, /not yet stored/);
});

test('concurrent expiration does not invalidate the new session', async t => {
  const f = await fixture(t, (_body, req) => req.headers['mcp-session-id'] === 'session-1'
    ? {status:404, text:'Session not found'} : {result:{content:[{type:'text',text:'ok'}]}});
  const client = new GraphitiMcpClient({url:f.url});
  await client.ensureInitialized();
  const results = await Promise.all([client.callTool('get_status'), client.callTool('get_status')]);
  assert.equal(results.length, 2);
  assert.equal(f.handshakes(), 2);
});

for (const scenario of ['ack', 'application', 'ambiguous'] as const) {
  test(`graph add reports ${scenario} truthfully, not fabricated queued success`, async t => {
    const f = await fixture(t, body => body.params.name === 'get_status'
      ? {result:{content:[{type:'text', text:'{"status":"ok"}'}]}}
      : scenario === 'application'
        ? {result:{content:[{type:'text', text:'{"error":"invalid_schema"}'}]}}
        : scenario === 'ambiguous'
          ? {status:500, text:'unknown after admission'}
          : {result:{structuredContent:{result:{status:'queued', uuid:'ack-id', message:'not yet stored in Neo4j'}}}});
    const backend = new GraphitiBackend({url:f.url, groupId:'diagnostic', projectGroupId:null,
      projectScoping:false, timeoutMs:1000});
    let tool: any;
    registerGraphitiTool({registerTool(value:any){tool=value;}} as any, backend, {spoolEnabled:false} as any);
    const value = await tool.execute('call-id', {action:'add', content:'body'}, new AbortController().signal);
    const payload = JSON.parse(value.content[0].text);
    if (scenario === 'ack') {
      assert.equal(payload.uuid, 'ack-id');
      assert.equal(payload.status, 'queued');
      assert.equal(payload.stored, false);
      assert.match(payload.message, /not yet stored/);
    } else {
      assert.equal(payload.success, false);
      assert.equal(payload.status, scenario === 'application' ? 'failed' : 'unknown');
      assert.equal(payload.spooled ?? false, false);
    }
  });
}

test('spool retains UUID on replay and holds ambiguous writes without blind replay', async t => {
  const root = mkdtempSync(join(tmpdir(), 'pi-graphiti-test-'));
  const old = process.env.PI_CODING_AGENT_DIR;
  process.env.PI_CODING_AGENT_DIR = root;
  t.after(() => { if(old === undefined) delete process.env.PI_CODING_AGENT_DIR; else process.env.PI_CODING_AGENT_DIR=old; rmSync(root,{recursive:true,force:true}); });
  const f = await fixture(t, body => ({result:{content:[{type:'text',text:'{"status":"queued"}'}]}}));
  const backend = new GraphitiBackend({url:f.url, groupId:'diagnostic', projectGroupId:null, projectScoping:false, timeoutMs:1000});
  enqueue({name:'safe',body:'body',groupId:'diagnostic',source:'text',uuid:'stable-id'} as any);
  enqueue({name:'unknown',body:'body',groupId:'diagnostic',source:'text',uuid:'held-id',manualReview:true} as any);
  const result = await drain(backend);
  assert.equal(result.replayed, 1);
  assert.equal(f.calls.length, 1);
  assert.equal(f.calls[0].params.arguments.uuid, 'stable-id');
  const held = JSON.parse(readFileSync(spoolFile(),'utf8').trim());
  assert.equal(held.uuid, 'held-id');
  assert.equal(held.manualReview, true);
});

test('automatic snapshot retains ambiguous write identity and does not replay', async t => {
  const root = mkdtempSync(join(tmpdir(), 'pi-graphiti-sync-test-'));
  const old = process.env.PI_CODING_AGENT_DIR;
  process.env.PI_CODING_AGENT_DIR = root;
  t.after(() => { if(old === undefined) delete process.env.PI_CODING_AGENT_DIR; else process.env.PI_CODING_AGENT_DIR=old; rmSync(root,{recursive:true,force:true}); });
  const f = await fixture(t, body => body.params.name === 'get_status'
    ? {result:{content:[{type:'text',text:'{"status":"ok"}'}]}}
    : {status:500,text:'may have accepted'});
  const backend = new GraphitiBackend({url:f.url,groupId:'diagnostic',projectGroupId:null,projectScoping:false,timeoutMs:1000});
  const handlers: Record<string,Function> = {};
  setupGraphitiSync({on(name:string, fn:Function){handlers[name]=fn;}} as any,backend,
    {flushOnCompact:true,flushMinTurns:1,spoolEnabled:true,spoolMaxEntries:200,spoolMaxBytes:8000000,spoolMaxAgeDays:14} as any);
  await handlers.message_end({message:{role:'user'}},{});
  await handlers.session_before_compact({}, {sessionManager:{getBranch:()=>[
    {type:'message',message:{role:'user',content:'private user'}},
    {type:'message',message:{role:'assistant',content:[{type:'text',text:'private assistant'}]}}
  ]}});
  const entry = JSON.parse(readFileSync(spoolFile(),'utf8').trim());
  assert.equal(entry.uuid, f.calls[1].params.arguments.uuid);
  assert.equal(entry.manualReview,true);
  assert.equal((await drain(backend)).replayed,0);
  assert.equal(f.calls.length,2);
});

test('MCP union result wrappers still produce entities/facts/episodes', async t => {
  const f = await fixture(t, body => ({result:{content:[{type:'text',text:JSON.stringify({result:
    body.params.name === 'search_nodes' ? {nodes:[{uuid:'n1',name:'Linh'}]} :
    body.params.name === 'search_memory_facts' ? {facts:[{uuid:'f1',fact:'Linh works for Lab'}]} :
    {episodes:[{uuid:'e1',name:'diagnostic',content:'body'}]}
  })}]}}));
  const backend = new GraphitiBackend({url:f.url,groupId:'diagnostic',projectGroupId:null,projectScoping:false,timeoutMs:1000});
  assert.equal((await backend.searchNodes('Linh'))[0]?.label,'Linh');
  assert.equal((await backend.searchFacts('Linh'))[0]?.summary,'Linh works for Lab');
  assert.equal((await backend.getEpisodes())[0]?.uuid,'e1');
});

test('concurrent callers wait for the entire initialization handshake', async t => {
  let markStarted!: () => void;
  const started = new Promise<void>(resolve => {markStarted=resolve;});
  let finished = false;
  let postedEarly = false;
  const f = await fixture(t, () => {
    if (!finished) postedEarly = true;
    return {result:{content:[{type:'text',text:'ok'}]}};
  }, async () => {
    markStarted();
    await new Promise(resolve => setTimeout(resolve, 40));
    finished = true;
  });
  const client = new GraphitiMcpClient({url:f.url});
  const handshake = client.ensureInitialized();
  await started;
  await Promise.all([handshake, client.callTool('get_status')]);
  assert.equal(postedEarly,false);
  assert.equal(f.handshakes(),1);
});

test('legacy spool without admission identity is held, never auto-replayed on activation', async t => {
  const root = mkdtempSync(join(tmpdir(), 'pi-graphiti-legacy-test-'));
  const old = process.env.PI_CODING_AGENT_DIR;
  process.env.PI_CODING_AGENT_DIR = root;
  t.after(() => { if(old === undefined) delete process.env.PI_CODING_AGENT_DIR; else process.env.PI_CODING_AGENT_DIR=old; rmSync(root,{recursive:true,force:true}); });
  const f = await fixture(t, () => ({result:{content:[{type:'text',text:'{"status":"queued"}'}]}}));
  const backend = new GraphitiBackend({url:f.url,groupId:'diagnostic',projectGroupId:null,projectScoping:false,timeoutMs:1000});
  mkdirSync(spoolDir(),{recursive:true});
  writeFileSync(spoolFile(), JSON.stringify({v:1,ts:Date.now(),name:'old snapshot',body:'old body',groupId:'diagnostic',source:'message'})+'\n');
  assert.equal((await drain(backend)).replayed,0);
  const first = JSON.parse(readFileSync(spoolFile(),'utf8').trim());
  assert.equal(first.manualReview,true);
  assert.equal(typeof first.uuid,'string');
  assert.equal((await drain(backend)).replayed,0);
  assert.equal(JSON.parse(readFileSync(spoolFile(),'utf8').trim()).uuid,first.uuid);
  assert.equal(f.calls.length,0);
});
