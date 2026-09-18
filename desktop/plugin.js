import {
  Button,
  COMPOSER_AREAS,
  PALETTE_AREA,
  ROUTES_AREA,
  Textarea,
  haptic,
  host
} from '@hermes/plugin-sdk'
import { useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'ai-budget-balancer'
const ROUTE = '/ai-budget-balancer/new'
const STORAGE_KEY = 'balancedSessions'

function balancedSessions(ctx) {
  const stored = ctx.storage.get(STORAGE_KEY, [])
  return Array.isArray(stored) ? stored.filter(value => typeof value === 'string') : []
}

function rememberBalancedSession(ctx, storedSessionId) {
  const next = [...new Set([...balancedSessions(ctx), storedSessionId])].slice(-200)
  ctx.storage.set(STORAGE_KEY, next)
}

export async function createBalancedConversation(ctx, text) {
  const prompt = String(text || '').trim()
  if (!prompt) throw new Error('Digite a primeira mensagem da conversa.')

  const decision = await ctx.rest('/route', {
    method: 'POST',
    body: { text: prompt, phase: 'new' },
    timeoutMs: 35_000
  })
  if (!decision?.ok || !decision.model || !decision.provider) {
    throw new Error('O administrador de cotas não retornou uma rota válida.')
  }

  const created = await host.request('session.create', {
    cols: 120,
    cwd: host.state.cwd.get() || '.',
    source: 'desktop',
    title: 'Conversa balanceada',
    model: decision.model,
    provider: decision.provider
  })
  if (!created?.session_id || !created?.stored_session_id) {
    throw new Error('O Hermes não conseguiu criar a sessão balanceada.')
  }

  await host.request('prompt.submit', {
    session_id: created.session_id,
    text: prompt
  })
  rememberBalancedSession(ctx, created.stored_session_id)
  await host.openSession(created.stored_session_id)

  return {
    runtimeSessionId: created.session_id,
    storedSessionId: created.stored_session_id,
    model: decision.model,
    provider: decision.provider,
    reason: decision.reason
  }
}

function BalancedConversationPage({ ctx }) {
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const submit = async () => {
    if (busy) return
    setBusy(true)
    setError('')
    try {
      const result = await createBalancedConversation(ctx, text)
      haptic('success')
      host.notify({
        kind: 'success',
        title: 'Conversa balanceada criada',
        message: `${result.provider === 'anthropic' ? 'Claude' : 'GPT'} foi escolhido para esta sessão.`
      })
    } catch (failure) {
      const message = failure instanceof Error ? failure.message : String(failure)
      setError(message)
      host.notifyError(failure, 'Não foi possível criar a conversa balanceada.')
    } finally {
      setBusy(false)
    }
  }

  return jsxs('div', {
    className: 'flex h-full max-w-3xl flex-col gap-4 p-5',
    children: [
      jsxs('div', {
        className: 'flex flex-col gap-1',
        children: [
          jsx('h1', { className: 'text-lg font-semibold', children: 'Conversa balanceada' }),
          jsx('p', {
            className: 'text-sm text-(--ui-text-tertiary)',
            children:
              'O Hermes compara os saldos, a proximidade das renovações e a adequação da tarefa antes de criar a sessão.'
          })
        ]
      }),
      jsx(Textarea, {
        value: text,
        onChange: event => setText(event.target.value),
        placeholder: 'Digite a primeira mensagem…',
        rows: 10,
        disabled: busy,
        autoFocus: true
      }),
      error
        ? jsx('div', {
            className: 'text-sm text-(--ui-danger)',
            role: 'alert',
            children: error
          })
        : null,
      jsx('div', {
        className: 'flex justify-end',
        children: jsx(Button, {
          onClick: () => void submit(),
          disabled: busy || !text.trim(),
          children: busy ? 'Calculando rota…' : 'Criar conversa'
        })
      })
    ]
  })
}

async function evaluateBalancedTurn(ctx, draft) {
  const storedSessionId = host.state.focusedStoredSessionId.get()
  const runtimeSessionId = host.state.focusedSessionId.get()
  if (!storedSessionId || !runtimeSessionId || !balancedSessions(ctx).includes(storedSessionId)) {
    return draft
  }

  try {
    const decision = await ctx.rest('/route', {
      method: 'POST',
      body: {
        text: String(draft.text || ''),
        phase: 'turn',
        current_model: host.state.model.get() || '',
        session_id: runtimeSessionId
      },
      timeoutMs: 35_000
    })
    if (!decision?.ok || !decision.switch_model) return draft

    const switched = await host.request('config.set', {
      session_id: runtimeSessionId,
      key: 'model',
      value: `${decision.model} --provider ${decision.provider} --session`
    })
    if (switched?.confirm_required) {
      host.notify({
        kind: 'warning',
        title: 'Troca automática bloqueada',
        message: switched.confirm_message || 'O Hermes exige confirmação manual para esse modelo.'
      })
      return draft
    }
    host.notify({
      kind: 'info',
      title: 'Modelo da conversa ajustado',
      message: `${decision.provider === 'anthropic' ? 'Claude' : 'GPT'} assumirá a partir deste turno para preservar a cota.`
    })
  } catch (failure) {
    host.notifyError(failure, 'A avaliação de cotas falhou; o modelo atual foi mantido.')
  }
  return draft
}

export default {
  id: ID,
  name: 'AI Budget Balancer',
  defaultEnabled: false,
  register(ctx) {
    ctx.registerMany([
      {
        id: 'new-balanced-conversation',
        area: PALETTE_AREA,
        data: {
          id: 'ai-budget-balancer.new',
          label: 'Nova conversa balanceada',
          keywords: ['gpt', 'claude', 'saldo', 'cota', 'balancear'],
          run: () => {
            haptic('tap')
            host.navigate(ROUTE)
          }
        }
      },
      {
        id: 'new-balanced-route',
        area: ROUTES_AREA,
        data: { path: ROUTE },
        render: () => jsx(BalancedConversationPage, { ctx })
      },
      {
        id: 'turn-router',
        area: COMPOSER_AREAS.middleware,
        order: 20,
        data: {
          handler: draft => evaluateBalancedTurn(ctx, draft)
        }
      }
    ])
  }
}
