import { JSDOM } from 'jsdom'
const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://localhost/' })
for (const name of ['window', 'document', 'HTMLElement', 'Event']) {
  globalThis[name] = dom.window[name]
}
globalThis.IS_REACT_ACT_ENVIRONMENT = true
