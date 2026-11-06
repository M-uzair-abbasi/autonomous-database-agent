import { useEffect, useState } from 'react'
import { getSchema } from '../api.js'

export default function HealthView({ health, onRefresh }) {
  const [schema, setSchema] = useState(null)

  useEffect(() => {
    let live = true
    getSchema().then((result) => {
      if (live) setSchema(result)
    })
    return () => {
      live = false
    }
  }, [])

  const data = health?.data ?? {}
  const reachable = Boolean(health?.ok && data.database_reachable)

  return (
    <>
      <section className="panel">
        <h2>Connection</h2>
        {health && !health.ok ? (
          <p className="notice">{health.error}</p>
        ) : (
          <dl className="kv">
            <dt>Database</dt>
            <dd>
              <span className="state" data-tone={reachable ? 'ok' : 'stop'}>
                {reachable ? 'connected' : 'unreachable'}
              </span>
              {data.database_error ? ` — ${data.database_error}` : ''}
            </dd>

            <dt>Dialect</dt>
            <dd>{data.dialect_prompt_name ?? '—'}</dd>

            <dt>Model stage</dt>
            <dd>
              <span className="state" data-tone={data.stage === 'stage-3-finetuned' ? 'ok' : 'warn'}>
                {data.stage ?? '—'}
              </span>
            </dd>

            <dt>Serving</dt>
            <dd>{data.served_model ?? '—'}</dd>

            <dt>Endpoint</dt>
            <dd>{data.base_url ?? 'built in (no model server)'}</dd>
          </dl>
        )}
        <div className="ask-row">
          <button className="example" type="button" onClick={onRefresh}>
            Refresh now
          </button>
          <span className="hint">Refreshes on its own every 15 seconds</span>
        </div>
      </section>

      <section className="panel">
        <h2>Schema in use</h2>
        {!schema ? (
          <p className="hint">Loading…</p>
        ) : !schema.ok ? (
          <p className="notice">{schema.error}</p>
        ) : (
          <SchemaTables tables={schema.data?.tables ?? {}} />
        )}
      </section>
    </>
  )
}

function SchemaTables({ tables }) {
  const names = Object.keys(tables)
  if (!names.length) {
    return <p className="empty">No tables found in the connected database.</p>
  }
  return (
    <ul className="timeline">
      {names.map((name) => {
        const table = tables[name] ?? {}
        const columns = table.columns ?? []
        const keys = table.foreign_keys ?? []
        return (
          <li key={name}>
            <span className="what">{name}</span>
            <span className="where">
              {columns.length} column{columns.length === 1 ? '' : 's'}
              {keys.length ? ` · ${keys.length} relationship${keys.length === 1 ? '' : 's'}` : ''}
            </span>
          </li>
        )
      })}
    </ul>
  )
}
