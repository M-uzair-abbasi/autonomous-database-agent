import { useEffect, useRef, useState } from 'react'
import { askQuestion } from '../api.js'

const EXAMPLES = [
  'How many customers do we have?',
  'Which products are out of stock?',
  'Which three products made the most money?',
  'How many orders has each customer placed?',
]

export default function AskView() {
  const [question, setQuestion] = useState('')
  const [pending, setPending] = useState(false)
  const [result, setResult] = useState(null)
  const [asked, setAsked] = useState('')
  const openedWith = useRef(false)

  async function submit(text) {
    const value = (text ?? question).trim()
    if (!value || pending) return
    setPending(true)
    setAsked(value)
    const answer = await askQuestion(value)
    setResult(answer)
    setPending(false)
    // Put the question in the address bar so a result can be linked to or
    // bookmarked — useful when someone wants to show a colleague exactly what
    // they asked, and what SQL came back.
    const url = new URL(window.location.href)
    url.searchParams.set('q', value)
    window.history.replaceState(null, '', url)
  }

  // A question in the URL runs once on open.
  useEffect(() => {
    if (openedWith.current) return
    openedWith.current = true
    const initial = new URLSearchParams(window.location.search).get('q')
    if (initial && initial.trim()) {
      setQuestion(initial)
      submit(initial)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <>
      <form
        onSubmit={(event) => {
          event.preventDefault()
          submit()
        }}
      >
        <label className="stage-label" htmlFor="question">
          Ask in plain English
        </label>
        <textarea
          id="question"
          className="ask-field"
          value={question}
          placeholder="How many customers do we have?"
          onChange={(event) => setQuestion(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) submit()
          }}
        />
        <div className="ask-row">
          <button className="ask-submit" type="submit" disabled={pending || !question.trim()}>
            {pending ? 'Translating…' : 'Ask'}
          </button>
          <span className="hint">Ctrl/⌘ + Enter also submits</span>
        </div>
        <div className="examples">
          {EXAMPLES.map((example) => (
            <button
              key={example}
              type="button"
              className="example"
              onClick={() => {
                setQuestion(example)
                submit(example)
              }}
            >
              {example}
            </button>
          ))}
        </div>
      </form>

      {result && <Answer asked={asked} result={result} />}
    </>
  )
}

function Answer({ asked, result }) {
  const data = result.data ?? {}
  const verdict = data.safety ?? null
  const allowed = verdict?.allowed

  // The API is unreachable, or failed before producing anything to show.
  if (!result.data) {
    return (
      <div className="reveal">
        <p className="echo">{asked}</p>
        <p className="notice">{result.error}</p>
      </div>
    )
  }

  return (
    <div className="reveal">
      <p className="echo">{asked}</p>

      {data.sql ? (
        <>
          <p className="stage-label">Translated to SQL</p>
          <pre className="sql">{data.sql}</pre>
        </>
      ) : (
        <p className="notice">{data.error || result.error || 'No SQL was produced.'}</p>
      )}

      {verdict && (
        <div className="gate" data-verdict={allowed ? 'allowed' : 'denied'}>
          <span className="gate-mark">{allowed ? 'PASSED' : 'REFUSED'}</span>
          <span className="gate-body">
            {allowed ? (
              <>
                Read&#8209;only check <strong>passed</strong>. The exact statement above was
                executed — nothing else was sent to the database.
              </>
            ) : (
              <>
                Read&#8209;only check <strong>refused</strong> this statement, so it never ran.{' '}
                {verdict.reason}
              </>
            )}
          </span>
        </div>
      )}

      {data.sql && !verdict && data.error && <p className="notice">{data.error}</p>}

      {allowed && Array.isArray(data.rows) && <Rows columns={data.columns} rows={data.rows} truncated={data.truncated} />}

      {allowed && data.error && <p className="notice">{data.error}</p>}
    </div>
  )
}

function Rows({ columns, rows, truncated }) {
  if (!rows.length) {
    return (
      <>
        <p className="stage-label">Result</p>
        <p className="empty">
          The query ran and matched no rows. That usually means a filter value does not
          appear in the data — check spelling and capitalisation.
        </p>
      </>
    )
  }

  return (
    <>
      <p className="stage-label">Result</p>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              {(columns ?? []).map((name, index) => (
                <th key={`${name}-${index}`}>{name}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, rowIndex) => (
              <tr key={rowIndex}>
                {row.map((cell, cellIndex) => (
                  <td key={cellIndex} className={cell === null ? 'null' : undefined}>
                    {cell === null ? 'null' : String(cell)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="rowcount">
        {rows.length} row{rows.length === 1 ? '' : 's'}
        {truncated ? ' · truncated' : ''}
      </p>
    </>
  )
}
