import { useCallback, useEffect, useState } from 'react'
import { getHealingAudit } from '../api.js'

// The journal records these; each maps to one meaning so the dot colour is
// never decorative.
const TONE = {
  succeeded: 'ok',
  verified: 'ok',
  resolved: 'ok',
  simulated: 'warn',
  planned: 'warn',
  executing: 'warn',
  detected: 'warn',
  outcome_unknown: 'warn',
  failed: 'stop',
  precondition_rejected: 'stop',
}

export default function HealingView() {
  const [audit, setAudit] = useState(null)

  const refresh = useCallback(async () => {
    setAudit(await getHealingAudit())
  }, [])

  useEffect(() => {
    refresh()
  }, [refresh])

  if (!audit) return <p className="hint">Loading…</p>
  if (!audit.ok) return <p className="notice">{audit.error}</p>

  const data = audit.data ?? {}

  if (!data.available) {
    return (
      <section className="panel">
        <h2>Nothing recorded yet</h2>
        <p className="empty">
          The healing engine has not run, so it has not observed this database and there
          is genuinely nothing to show. Start it with{' '}
          <code>python3 -m scm_agent.healing once</code> and this screen fills in.
        </p>
        <p className="empty">
          It starts in <code>observe</code> mode: it records what it finds and changes
          nothing until you tell it otherwise.
        </p>
      </section>
    )
  }

  return (
    <>
      <section className="panel">
        <h2>Actions taken</h2>
        {data.actions?.length ? (
          <ul className="timeline">
            {data.actions.map((action) => (
              <li key={`action-${action.id}`}>
                <time>{shortTime(latestAt(action))}</time>
                <span className="what">{action.action}</span>
                <span className="where">{action.target_key}</span>
                <span className="state" data-tone={TONE[action.state] ?? undefined}>
                  {action.state ?? 'unknown'}
                </span>
                <span className="where">{action.mode}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="empty">
            No corrective action has been taken. In <code>observe</code> mode the engine
            records what it finds and changes nothing.
          </p>
        )}
      </section>

      <section className="panel">
        <h2>Anomalies detected</h2>
        {data.events?.length ? (
          <ul className="timeline">
            {data.events.map((event) => (
              <li key={`event-${event.id}`}>
                <time>{shortTime(latestAt(event))}</time>
                <span className="what">{event.rule}</span>
                <span className="where">{event.target_key}</span>
                <span className="state" data-tone={TONE[event.state] ?? undefined}>
                  {event.state ?? 'unknown'}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="empty">No anomalies recorded. The database looks healthy.</p>
        )}
      </section>

      <div className="ask-row">
        <button className="example" type="button" onClick={refresh}>
          Refresh
        </button>
        <span className="hint">
          Read-only view. Healing runs from the command line, never from this page.
        </span>
      </div>
    </>
  )
}

function latestAt(record) {
  const transitions = record.transitions ?? []
  return transitions.length ? transitions[transitions.length - 1].at : null
}

function shortTime(value) {
  if (!value) return '—'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return String(value).slice(0, 16)
  return parsed.toLocaleString(undefined, {
    month: 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}
