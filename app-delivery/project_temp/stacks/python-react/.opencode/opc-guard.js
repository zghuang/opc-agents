import { spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import path from 'node:path'

const MUTATING_TOOLS = new Set(['write', 'edit', 'bash'])

function guardPath() {
  const home = process.env.HOME || ''
  if (!home) return null
  const candidates = [
    path.join(home, 'apps', 'agents', 'opc-agents', 'app-delivery', 'scripts', 'app_delivery_pre_tool_guard.py'),
    path.join(home, 'opc', 'opc-agents', 'app-delivery', 'scripts', 'app_delivery_pre_tool_guard.py'),
  ]
  return candidates.find((candidate) => existsSync(candidate)) || null
}

function normalizeArgs(tool, args) {
  if (tool === 'bash') {
    return {
      command: String(args?.command || ''),
      workdir: typeof args?.workdir === 'string' ? args.workdir : '',
    }
  }
  if (tool === 'write' || tool === 'edit') {
    return {
      filePath: String(args?.filePath || args?.path || ''),
      path: String(args?.path || args?.filePath || ''),
      content: typeof args?.content === 'string' ? args.content : '',
    }
  }
  return args
}

async function OpcGuardPlugin(input) {
  const guard = guardPath()
  if (!guard) {
    return {}
  }

  return {
    'tool.execute.before': async ({ tool }, output) => {
      if (!MUTATING_TOOLS.has(tool)) {
        return
      }

      const payload = {
        tool_name: tool,
        cwd: input.directory,
        tool_input: normalizeArgs(tool, output.args),
      }

      const completed = spawnSync('python3', [guard], {
        input: JSON.stringify(payload),
        encoding: 'utf8',
      })

      if (completed.error) {
        throw completed.error
      }

      const text = String(completed.stdout || '').trim()
      if (!text) {
        return
      }

      try {
        const decision = JSON.parse(text)
        if (decision && decision.decision === 'block') {
          throw new Error(String(decision.reason || 'OPC pre-tool guard blocked the operation.'))
        }
      } catch (error) {
        if (error instanceof SyntaxError) {
          throw new Error(`OPC pre-tool guard returned invalid output: ${text}`)
        }
        throw error
      }
    },
  }
}

export default OpcGuardPlugin