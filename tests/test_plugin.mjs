import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'

const pluginUrl = new URL('../desktop/plugin.js', import.meta.url)

const plain = value => JSON.parse(JSON.stringify(value))

function atom(value) {
  return { get: () => value }
}

async function loadPlugin(hostOverrides = {}) {
  const requests = []
  const opened = []
  const navigated = []
  const notifications = []
  const host = {
    state: {
      cwd: atom('/workspace'),
      model: atom('gpt-5.6-sol'),
      focusedSessionId: atom('runtime-current'),
      focusedStoredSessionId: atom('stored-current')
    },
    request: async (method, params) => {
      requests.push([method, params])
      if (method === 'session.create') {
        return { session_id: 'runtime-new', stored_session_id: 'stored-new' }
      }
      return { status: 'streaming' }
    },
    openSession: async id => opened.push(id),
    navigate: path => navigated.push(path),
    notify: value => notifications.push(value),
    notifyError: value => notifications.push({ error: String(value) }),
    ...hostOverrides
  }

  const sdk = {
    host,
    PALETTE_AREA: 'palette',
    ROUTES_AREA: 'routes',
    COMPOSER_AREAS: { middleware: 'composer.middleware' },
    Button: 'Button',
    Textarea: 'Textarea',
    Badge: 'Badge',
    Tip: 'Tip',
    cn: (...items) => items.filter(Boolean).join(' '),
    haptic: () => undefined
  }
  const react = { useState: initial => [initial, () => undefined] }
  const runtime = {
    jsx: (type, props) => ({ type, props }),
    jsxs: (type, props) => ({ type, props })
  }
  const context = vm.createContext({ console, setTimeout, clearTimeout })
  const source = await fs.readFile(pluginUrl, 'utf8')
  const module = new vm.SourceTextModule(source, { context, identifier: pluginUrl.href })

  await module.link(async specifier => {
    const values = specifier === '@hermes/plugin-sdk' ? sdk : specifier === 'react' ? react : runtime
    const names = Object.keys(values)
    return new vm.SyntheticModule(
      names,
      function () {
        for (const name of names) this.setExport(name, values[name])
      },
      { context, identifier: specifier }
    )
  })
  await module.evaluate()
  return { namespace: module.namespace, host, requests, opened, navigated, notifications }
}

function makeContext(rest, initialStorage = {}) {
  const contributions = []
  const storage = { ...initialStorage }
  return {
    contributions,
    storage,
    ctx: {
      register: contribution => contributions.push(contribution),
      registerMany: items => contributions.push(...items),
      rest,
      storage: {
        get: (key, fallback) => (key in storage ? storage[key] : fallback),
        set: (key, value) => {
          storage[key] = value
        },
        remove: key => delete storage[key]
      }
    }
  }
}

test('creates a model-pinned session and submits the first prompt', async () => {
  const loaded = await loadPlugin()
  const fake = makeContext(async path => {
    assert.equal(path, '/route')
    return { ok: true, model: 'claude-sonnet-5', provider: 'anthropic', reason: 'task_fit' }
  })

  const result = await loaded.namespace.createBalancedConversation(fake.ctx, 'Implemente com testes')

  assert.equal(result.storedSessionId, 'stored-new')
  assert.deepEqual(plain(loaded.requests[0]), [
    'session.create',
    {
      cols: 120,
      cwd: '/workspace',
      source: 'desktop',
      title: 'Conversa balanceada',
      model: 'claude-sonnet-5',
      provider: 'anthropic'
    }
  ])
  assert.deepEqual(plain(loaded.requests[1]), [
    'prompt.submit',
    { session_id: 'runtime-new', text: 'Implemente com testes' }
  ])
  assert.deepEqual(loaded.opened, ['stored-new'])
  assert.deepEqual(plain(fake.storage.balancedSessions), ['stored-new'])
})

test('does not remember a balanced session when the first prompt fails', async () => {
  const loaded = await loadPlugin({
    request: async method => {
      if (method === 'session.create') {
        return { session_id: 'runtime-new', stored_session_id: 'stored-new' }
      }
      throw new Error('submit failed')
    }
  })
  const fake = makeContext(async () => ({
    ok: true,
    model: 'gpt-5.6-sol',
    provider: 'openai-codex',
    reason: 'balanced_default'
  }))

  await assert.rejects(
    loaded.namespace.createBalancedConversation(fake.ctx, 'Primeira mensagem'),
    /submit failed/
  )
  assert.equal(fake.storage.balancedSessions, undefined)
})

test('registers the palette action, route and turn middleware', async () => {
  const loaded = await loadPlugin()
  const fake = makeContext(async () => ({ ok: true }), { balancedSessions: [] })
  loaded.namespace.default.register(fake.ctx)

  assert.deepEqual(
    fake.contributions.map(item => item.area).sort(),
    ['composer.middleware', 'palette', 'routes'].sort()
  )
  const command = fake.contributions.find(item => item.area === 'palette')
  command.data.run()
  assert.deepEqual(loaded.navigated, ['/ai-budget-balancer/new'])
})

test('middleware switches only a balanced session and keeps the change session-scoped', async () => {
  const loaded = await loadPlugin()
  const fake = makeContext(
    async (path, options) => {
      assert.equal(path, '/route')
      assert.equal(options.body.phase, 'turn')
      return {
        ok: true,
        switch_model: true,
        model: 'claude-sonnet-5',
        provider: 'anthropic',
        reason: 'projected_floor_breach'
      }
    },
    { balancedSessions: ['stored-current'] }
  )
  loaded.namespace.default.register(fake.ctx)
  const middleware = fake.contributions.find(item => item.area === 'composer.middleware')

  const draft = { text: 'continue', attachments: [] }
  assert.deepEqual(await middleware.data.handler(draft), draft)
  assert.deepEqual(plain(loaded.requests), [
    [
      'config.set',
      {
        session_id: 'runtime-current',
        key: 'model',
        value: 'claude-sonnet-5 --provider anthropic --session'
      }
    ]
  ])
})
