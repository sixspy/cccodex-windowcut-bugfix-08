export const auditViewTemplate = `
<div class="card" v-show="tab===5">
  <h2>审计流水</h2>
  <table>
    <thead><tr><th>时间</th><th>人</th><th>动作</th><th>目标</th></tr></thead>
    <tbody>
      <tr v-for="a in audits.slice(0,20)" :key="a.id">
        <td>{{a.created_at}}</td>
        <td>{{a.actor}}</td>
        <td>{{a.action}}</td>
        <td>{{a.target}}</td>
      </tr>
    </tbody>
  </table>
</div>
<div class="card" v-show="tab===5">
  <h2>争用 / 锁</h2>
  <p class="muted">争用 {{contention.length}} · 持锁 {{locks.length}}</p>
  <table>
    <thead><tr><th>锁键</th><th>角色</th><th>动作</th><th>时间</th></tr></thead>
    <tbody>
      <tr v-for="c in contention.slice(0,15)" :key="'c'+c.id">
        <td>{{c.lock_key}}</td>
        <td>{{c.actor}}</td>
        <td>{{c.action}}</td>
        <td>{{c.created_at}}</td>
      </tr>
    </tbody>
  </table>
  <h2 style="margin-top:16px">持锁持有人</h2>
  <table>
    <thead><tr><th>锁键</th><th>持有人</th><th>取得时间</th></tr></thead>
    <tbody>
      <tr v-for="l in locks" :key="'l'+l.lock_key">
        <td>{{l.lock_key}}</td><td>{{l.holder}}</td><td>{{l.acquired_at}}</td>
      </tr>
    </tbody>
  </table>
  <h2 style="margin-top:16px">票根回执</h2>
  <table>
    <thead><tr><th>票根</th><th>锁键</th><th>请求人</th><th>持有人</th><th>结果</th></tr></thead>
    <tbody>
      <tr v-for="t in receipts.slice(0,10)" :key="'t'+t.id">
        <td>{{t.ticket}}</td><td>{{t.lock_key}}</td><td>{{t.actor}}</td>
        <td>{{t.holder||'—'}}</td>
        <td :class="t.outcome==='持有人释锁'||t.outcome==='持有'?'ok':'late'">{{t.outcome}}</td>
      </tr>
    </tbody>
  </table>
  <p class="muted">释锁册口径：{{ledger.path||'—'}}（{{ledger.decided_on||'—'}}，{{ledger.consistent?'与当前实现一致':'偏离'}}）</p>
  <h2 style="margin-top:16px">旁路暂存</h2>
  <label>窗口</label><input v-model="holdForm.window_name"/>
  <label>业务键</label><input v-model="holdForm.biz_key"/>
  <label>时间</label><input v-model="holdForm.event_ts"/>
  <label>金额</label><input type="number" v-model.number="holdForm.amount"/>
  <button class="primary" @click="createHold">写入暂存</button>
  <table>
    <thead><tr><th>ID</th><th>键</th><th>金额</th><th>状态</th></tr></thead>
    <tbody>
      <tr v-for="h in holds" :key="'h'+h.id">
        <td>{{h.id}}</td><td>{{h.biz_key}}</td><td>{{h.amount}}</td><td>{{h.status}}</td>
      </tr>
    </tbody>
  </table>
</div>
`;
