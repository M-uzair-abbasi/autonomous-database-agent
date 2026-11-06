// Every network call the dashboard makes, in one place.
//
// Each returns { ok, status, data, error } rather than throwing. A dashboard
// that crashes because the backend is down is worse than one that says the
// backend is down, and every view here has a defined appearance for failure.

async function request(path, options) {
  try {
    const response = await fetch(path, options)
    let data = null
    try {
      data = await response.json()
    } catch {
      data = null
    }
    return {
      ok: response.ok,
      status: response.status,
      data,
      error: response.ok ? null : describe(response.status, data),
    }
  } catch (cause) {
    // fetch only rejects when the request never completed — the server is not
    // running, or the connection dropped. Worth saying so precisely.
    return {
      ok: false,
      status: 0,
      data: null,
      error: `Cannot reach the API. Is the backend running on port 8080? (${cause.message})`,
    }
  }
}

function describe(status, data) {
  if (data && typeof data.error === 'string' && data.error) return data.error
  if (data && typeof data.reason === 'string' && data.reason) return data.reason
  if (status === 400) return 'The request was rejected.'
  if (status >= 500) return 'The server could not complete the request.'
  return `Request failed (${status}).`
}

export const getHealth = () => request('/health')
export const getSchema = () => request('/schema?include_ddl=false')
export const getHealingAudit = () => request('/healing/audit')

export const askQuestion = (question, includePrompt = false) =>
  request('/query', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question, include_prompt: includePrompt }),
  })
