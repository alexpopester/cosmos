// Prints a hash of everything the tool build reads. "pnpm run build" writes it to
// tools/flowautomator/.source-hash; the Rakefile computes the same hash (tool_source_hash)
// to check a prebuilt tool is current when it runs without Node (openc3.sh cli rake build).
import { createHash } from 'node:crypto'
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'

function walk(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name).split('\\').join('/')
    return statSync(path).isDirectory() ? walk(path) : [path]
  })
}

const sha = (data) => createHash('sha256').update(data).digest('hex')
const files = [...walk('src'), ...['package.json', 'pnpm-lock.yaml', 'vite.config.js'].filter((f) => existsSync(f))]
  .map((f) => Buffer.from(f))
  .sort(Buffer.compare)
  .map((f) => f.toString())
process.stdout.write(sha(files.map((f) => `${f}:${sha(readFileSync(f))}`).join('\n')))
