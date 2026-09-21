/**
 * 离线验收：不启动 harness，直接跑插件的每个工具。
 *
 * 验三件事：
 *   1. 参数 schema 能被 defineTool 编译，且非法输入会被挡下
 *   2. 每个工具真能调到规则层、返回真实结果
 *   3. 返回值能通过 output.schema 的校验（否则在 dsh 里会变成 isError）
 *
 * 用法：
 *   node --experimental-strip-types test/offline-check.mjs
 */
import { readFileSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'

import { validateJsonSchemaValue } from '@deepseek-ai/dsh-tools'

import { Config, createTools, name } from '../src/index.ts'

const OPEN_BOX = process.env.OPEN_BOX_ROOT ?? '/Users/a1234/Desktop/open_box'
const PYTHON = process.env.PYTHON_BIN
  ?? '/Users/a1234/.workbuddy/binaries/python/envs/default/bin/python'
const BRIDGE = fileURLToPath(new URL('../python/resume_rules.py', import.meta.url))

// schemastery 的默认值要靠它的解析器补全；这里直接给全，等价于 cordis.yml 里的配置
const config = { openBoxRoot: OPEN_BOX, pythonBin: PYTHON, timeoutMs: 60_000 }

/** 绕过工具 schema 直接问桥——用来验规则层的纵深覆盖。 */
function bridge(cmd, payload) {
  const proc = spawnSync(PYTHON, [BRIDGE], {
    input: JSON.stringify({ cmd, ...payload }),
    encoding: 'utf-8',
    env: { ...process.env, PYTHONPATH: OPEN_BOX, PYTHONIOENCODING: 'utf-8' },
  })
  const env = JSON.parse(proc.stdout)
  if (!env.ok) throw new Error(`桥报错：${env.error}`)
  return env.data
}

let passed = 0
let failed = 0

function check(label, condition, detail = '') {
  if (condition) {
    passed++
    console.log(`  ✓  ${label}`)
  } else {
    failed++
    console.log(`  ✗  ${label}${detail ? `\n       ${detail}` : ''}`)
  }
}

/** 跑一个工具：先验参数，再验输出 schema，最后跑渲染器。 */
async function run(tool, args, label) {
  const value = await tool.execute(args, { signal: undefined, agent: undefined })
  const violations = validateJsonSchemaValue(tool.output.schema, value, '')
  check(`${label} · 输出符合 output.schema`, violations.length === 0, violations.join('; '))
  const blocks = tool.output.render(args, value)
  check(`${label} · render 返回内容块`, Array.isArray(blocks) && blocks.every((b) => b.type === 'text'))
  return { value, text: blocks.map((b) => b.text).join('\n') }
}

const tools = createTools(config)
const byName = Object.fromEntries(tools.map((t) => [t.name, t]))

console.log(`\n插件 ${name}　配置 openBoxRoot=${OPEN_BOX} pythonBin=${PYTHON}\n`)

// ── 1. 工具注册 ──────────────────────────────────────────────────────────
console.log('【工具注册】')
const EXPECTED = [
  'resume_ingest', 'resume_validate_claim', 'resume_plan_queries',
  'resume_classify_tier', 'resume_judge_claim', 'resume_summary',
]
check(`注册了 ${EXPECTED.length} 个工具`, tools.length === EXPECTED.length, `实际 ${tools.length}`)
for (const n of EXPECTED) check(`工具 ${n} 存在`, n in byName)

// ── 2. 参数校验会挡下非法输入 ────────────────────────────────────────────
console.log('\n【参数校验】')
try {
  await byName.resume_validate_claim.execute(
    { claim: { raw_text: 'x', category: '不存在的类别' } },
    { signal: undefined, agent: undefined },
  )
  check('非法 category 应被挡下', false, '没有抛错')
} catch (err) {
  check('非法 category 被挡下', /category|enum|valid/i.test(String(err.message)), String(err.message))
}
try {
  await byName.resume_judge_claim.execute(
    { claim: { raw_text: 'x', category: '竞赛' }, evidences: [{ title: '缺 url' }] },
    { signal: undefined, agent: undefined },
  )
  check('证据缺 url 应被挡下', false, '没有抛错')
} catch (err) {
  check('证据缺 url 被挡下', true)
}

// ── 3. ingest ────────────────────────────────────────────────────────────
console.log('\n【resume_ingest】')
const ingest = await run(byName.resume_ingest, { path: `${OPEN_BOX}/samples/resume_lin.pdf` }, 'ingest')
check('检出隐藏文字与注入风险', ingest.value.risks.length >= 4, `实际 ${ingest.value.risks.length}`)
check('safe_text 比原文短（风险行已剔除）', ingest.value.safe_chars < ingest.value.raw_chars)
check('render 汇报了风险条数', ingest.text.includes(`${ingest.value.risks.length} 处`))
const meta = byName.resume_ingest.output.presentationMeta({ path: 'x' }, ingest.value)
check('presentationMeta 可序列化', typeof JSON.stringify(meta) === 'string')

// ── 4. plan ──────────────────────────────────────────────────────────────
console.log('\n【resume_plan_queries】')
const contestClaim = {
  id: 'c04', raw_text: '2023.11　ICPC 亚洲区域赛 晴川站 铜奖（队伍：晴川大学 Trailing Zeros）',
  raw_locator: '第2页·竞赛经历·第1行', category: '竞赛', date_label: '2023.11',
  date_start: '2023-11-01', date_end: '2023-11-30',
  elements: ['赛事=ICPC 亚洲区域赛 晴川站', '奖项=铜奖', '参赛身份=队伍 Trailing Zeros 成员', '时间=2023.11'],
  entities: { org: '晴川大学', team: 'Trailing Zeros', level: '区域赛' },
}
const plan = await run(byName.resume_plan_queries, { claim: contestClaim, candidate_name: '林昱和' }, 'plan')
check('生成了检索查询', plan.value.queries.length >= 2, `实际 ${plan.value.queries.length}`)
check('查询带域名限定', plan.value.queries.some((q) => q.site), JSON.stringify(plan.value.queries))

const eduPlan = await run(byName.resume_plan_queries, {
  claim: { id: 'c01', raw_text: '晴川大学 本科在读', category: '学历', date_label: '2022.09' },
}, 'plan(学历)')
check('学历按设计不检索', eduPlan.value.queries.length === 0)
check('学历给出不检索的理由', eduPlan.value.note.includes('学信网'))

// ── 5. classify_tier ─────────────────────────────────────────────────────
console.log('\n【resume_classify_tier】')
const tier = await run(byName.resume_classify_tier, {
  url: 'https://mp.weixin.qq.com/s/abc', title: '换届公告', publisher: 'x',
  wechat_verified_subject: '晴川大学',
}, 'tier(公众号)')
check('认证主体公众号判 B', tier.value.source_tier === 'B', tier.value.source_tier)

const tierNoSubject = await run(byName.resume_classify_tier, {
  url: 'https://mp.weixin.qq.com/s/abc', title: '换届公告', publisher: 'x',
}, 'tier(公众号但无认证主体)')
check('无认证主体就降级', tierNoSubject.value.source_tier !== 'B', tierNoSubject.value.source_tier)

const tierRepost = await run(byName.resume_classify_tier, {
  url: 'https://xsc.qingchuan.edu.cn/gongshi.html', title: '三好学生公示',
  snippet: '名单', is_repost: true,
}, 'tier(转载的公示)')
check('转载不算 A 级', tierRepost.value.source_tier !== 'A', tierRepost.value.source_tier)

// ── 6. judge：规则必须覆盖模型给的等级与身份分 ───────────────────────────
console.log('\n【resume_judge_claim】')

// 6.1 边界拒绝：模型连"填"的机会都不该有。
// schema 用 additionalProperties:false 把 source_tier / identity_score 挡在参数校验那一层，
// 这样落进会话日志的参数就等于模型真正提交的东西，不会悄悄被改写。
try {
  await byName.resume_judge_claim.execute({
    claim: contestClaim,
    evidences: [{
      url: 'https://x.example/a.html', title: 't', publisher: 'p', snippet: 's',
      source_tier: 'A', identity_score: 999,
    }],
  }, { signal: undefined, agent: undefined })
  check('模型自填 source_tier / identity_score 应被边界拒绝', false, '没有抛错')
} catch (err) {
  const msg = String(err.message)
  check('模型自填 identity_score 被边界拒绝',
    /identity_score/.test(msg) && /INVALID_ARGS|not a declared property/i.test(msg + err.code))
}

// 6.2 纵深覆盖：即使绕过工具 schema 直接问桥，规则层也必须无视这两个字段
const override = await bridge('judge', {
  claim: contestClaim,
  evidences: [{
    url: 'https://icpc-qingchuan.example/2023/awards.html',
    title: '获奖名单公示', publisher: 'ICPC 亚洲区域赛晴川站组委会', snippet: '铜奖',
    supports: ['奖项=铜奖'],
    source_tier: 'D',
    identity_score: 999,
  }],
  organizer_hosts: ['icpc-qingchuan.example'],
})
check('桥把模型填的 D 重算成 A', override.verified_claim.best_tier === 'A',
  String(override.verified_claim.best_tier))
check('桥把模型填的 999 冲掉（无身份信号 → 0）',
  override.verified_claim.evidence[0].identity_score === 0,
  String(override.verified_claim.evidence[0].identity_score))

// 6.3 正常路径
const judgeInput = {
  claim: contestClaim,
  evidences: [{
    url: 'https://icpc-qingchuan.example/2023/awards.html',
    title: '2023 ICPC 亚洲区域赛晴川站 获奖名单公示',
    publisher: 'ICPC 亚洲区域赛晴川站组委会',
    snippet: '铜奖　晴川大学　Trailing Zeros（陈屿、林昱和、周清让）　指导教师：方序',
    published_at: '2023-11-20',
    identity_signals: ['学校一致', '队友或合作者一致', '年级一致'],
    supports: [...contestClaim.elements],
  }],
  organizer_hosts: ['icpc-qingchuan.example'],
}
const judge = await run(byName.resume_judge_claim, judgeInput, 'judge')
check('判为已证实', judge.value.verified_claim.status === 'ok', judge.value.verified_claim.status)
check('来源等级 A', judge.value.verified_claim.best_tier === 'A', String(judge.value.verified_claim.best_tier))
check('身份分 = 2+2+1 = 5（学校/队友/年级）',
  judge.value.verified_claim.evidence[0].identity_score === 5,
  String(judge.value.verified_claim.evidence[0].identity_score))
check('要素全部证明', judge.value.verified_claim.unproved.length === 0)
const meta2 = byName.resume_judge_claim.output.presentationMeta(judgeInput, judge.value)
check('presentationMeta 给出前端要的字段',
  meta2.kind === 'verdict' && meta2.claimId === 'c04'
  && Array.isArray(meta2.evidence) && meta2.evidence[0].url.includes('icpc-qingchuan'))
check('presentationMeta 里的等级已经是规则算出来的',
  meta2.bestTier === 'A' && meta2.evidence[0].sourceTier === 'A')

// 6.4 自造要素必须被丢弃
const invented = await run(byName.resume_judge_claim, {
  claim: contestClaim,
  evidences: [{
    url: 'https://icpc-qingchuan.example/x.html', title: '获奖名单公示', publisher: 'x',
    snippet: '铜奖',
    identity_signals: ['学校一致', '队友或合作者一致'],
    supports: ['奖项=铜奖', '不存在的要素=编的'],
  }],
  organizer_hosts: ['icpc-qingchuan.example'],
}, 'judge(自造要素)')
check('证据里的自造要素被收窄',
  JSON.stringify(invented.value.verified_claim.evidence[0].supports) === '["奖项=铜奖"]',
  JSON.stringify(invented.value.verified_claim.evidence[0].supports))
check('自造要素没有被算进 proved',
  !invented.value.verified_claim.proved.includes('不存在的要素=编的'))
check('真实要素照常证明，缺的照常进 unproved',
  invented.value.verified_claim.proved.includes('奖项=铜奖')
  && invented.value.verified_claim.status === 'part',
  `${invented.value.verified_claim.status} / ${JSON.stringify(invented.value.verified_claim.proved)}`)
check('丢弃原因如实报出', invented.value.dropped.some((d) => d.includes('不存在的要素')),
  JSON.stringify(invented.value.dropped))

// 6.5 同名一票否决
const conflict = await run(byName.resume_judge_claim, {
  claim: { ...contestClaim, id: 'c09', entities: { org: '晴川大学' } },
  evidences: [{
    url: 'https://x.example/a.html', title: '获奖公示', publisher: 'x', snippet: '二等奖',
    identity_signals: ['学校一致', '学院一致', '年级一致'],
    identity_conflicts: ['学校不同：某师范大学'],
    supports: [...contestClaim.elements],
  }],
}, 'judge(同名冲突)')
check('有身份冲突一律判身份未确认',
  conflict.value.verified_claim.status === 'who', conflict.value.verified_claim.status)
check('身份未确认时仍需人工', conflict.value.verified_claim.needs_human === true)
check('身份分很高也拦得住（3 个信号 = 5 分仍判 who）', conflict.value.verified_claim.status === 'who')

// 6.6 查不到就是查不到
const empty = await run(byName.resume_judge_claim, { claim: contestClaim, evidences: [] }, 'judge(无证据)')
check('没有证据判未找到公开记录', empty.value.verified_claim.status === 'none')

const exhausted = await run(byName.resume_judge_claim,
  { claim: contestClaim, evidences: [], search_exhausted: true }, 'judge(预算耗尽)')
check('预算耗尽且无证据也判 none', exhausted.value.verified_claim.status === 'none')
check('search_exhausted 被如实记下', exhausted.value.verified_claim.search_exhausted === true)

// ── 7. summary ───────────────────────────────────────────────────────────
console.log('\n【resume_summary】')
const fixture = JSON.parse(readFileSync(`${OPEN_BOX}/fixtures/report_lin.json`, 'utf-8'))
const summary = await run(byName.resume_summary, { claims: fixture.claims }, 'summary')
check('总数正确', summary.value.total === 11, String(summary.value.total))
check('计数正确', summary.value.counts.ok === fixture.claims.filter((c) => c.status === 'ok').length)
check('必须带边界声明', summary.value.disclaimer.includes('不代表候选人的可信程度'))
check('render 带上了边界声明', summary.text.includes('不代表候选人的可信程度'))

// ── 收尾 ─────────────────────────────────────────────────────────────────
console.log('\n' + '─'.repeat(50))
console.log(`${passed} 项通过，${failed} 项失败`)
console.log(`Config schema 可编译：${typeof Config === 'function' || typeof Config === 'object'}`)
process.exit(failed === 0 ? 0 : 1)
