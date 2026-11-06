import { useCallback, useEffect, useState } from 'react'
import { getHealth } from './api.js'
import AskView from './views/AskView.jsx'
import HealthView from './views/HealthView.jsx'
import HealingView from './views/HealingView.jsx'

const TABS = [
  { id: 'ask', label: 'Ask' },
  { id: 'health', label: 'Database health' },
  { id: 'healing', label: 'Healing log' },
]

const TAB_IDS = TABS.map((entry) => entry.id)

function tabFromHash() {
  const candidate = window.location.hash.replace('#', '')
  return TAB_IDS.includes(candidate) ? candidate : 'ask'
}

export default function App() {
  // The open tab lives in the URL hash, so a screen can be linked to and the
  // browser's back button behaves the way people expect it to.
  const [tab, setTabState] = useState(tabFromHash)
  const [health, setHealth] = useState(null)

  const setTab = (next) => {
    setTabState(next)
    window.location.hash = next
  }

  useEffect(() => {
    const onHashChange = () => setTabState(tabFromHash())
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])

  const refreshHealth = useCallback(async () => {
    setHealth(await getHealth())
  }, [])

  // One poll on load and every 15s. The masthead chip is the only always-on
  // indicator, so it must not go stale while someone is reading a result.
  useEffect(() => {
    refreshHealth()
    const timer = setInterval(refreshHealth, 15000)
    return () => clearInterval(timer)
  }, [refreshHealth])

  const stage = health?.data?.stage
  const reachable = health?.ok && health?.data?.database_reachable

  return (
    <div className="shell">
      <header className="masthead">
        <h1 className="wordmark">
          SCM&#8209;OVS <span>/ database console</span>
        </h1>
        <span className="stage-chip" data-live={String(Boolean(reachable))}>
          {health === null
            ? 'connecting…'
            : !health.ok
              ? 'api unreachable'
              : `${stage ?? 'unknown stage'} · ${reachable ? 'db connected' : 'db unreachable'}`}
        </span>
      </header>

      <nav className="tabs" role="tablist" aria-label="Sections">
        {TABS.map((entry) => (
          <button
            key={entry.id}
            role="tab"
            className="tab"
            aria-selected={tab === entry.id}
            onClick={() => setTab(entry.id)}
          >
            {entry.label}
          </button>
        ))}
      </nav>

      <main>
        {tab === 'ask' && <AskView />}
        {tab === 'health' && <HealthView health={health} onRefresh={refreshHealth} />}
        {tab === 'healing' && <HealingView />}
      </main>
    </div>
  )
}
