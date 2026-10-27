"""Read-only semantic fallback for native controls in an application form."""
import logging

from selenium.common.exceptions import StaleElementReferenceException
from selenium.webdriver.common.by import By


SNAPSHOT_SCRIPT = r"""
const container = arguments[0], known = arguments[1];
const clean = value => (value || '').replace(/\s+/g, ' ').trim();
const visible = el => {
    if (!el || !el.isConnected || el.closest('[hidden],[inert],[aria-hidden="true"]')) return false;
    const rect = el.getBoundingClientRect();
    if (rect.width <= 1 || rect.height <= 1) return false;
    for (let p = el; p; p = p.parentElement) {
        const style = getComputedStyle(p), box = p.getBoundingClientRect();
        if (style.display === 'none' || style.visibility !== 'visible' || Number(style.opacity) === 0) return false;
        if (/(hidden|clip)/.test(style.overflowX) && Math.min(rect.right, box.right) - Math.max(rect.left, box.left) <= 1) return false;
        if (/(hidden|clip)/.test(style.overflowY) && Math.min(rect.bottom, box.bottom) - Math.max(rect.top, box.top) <= 1) return false;
    }
    return true;
};
const roots = container ? [container] : [...document.querySelectorAll(
    '[data-qa="vacancy-response-popup"], [data-qa="vacancy-response-form"], ' +
    '[data-qa="vacancy-response-submit-popup"], [data-qa="vacancy-response-letter-submit"]'
)].map(el => el.closest('form,[role="dialog"],[data-qa="vacancy-response-popup"]'));
if (!container) for (const block of known) roots.push(block.closest('form,[role="dialog"]'));
const excluded = el => el.closest('[data-qa*="letter"],[data-qa*="resume"],[data-qa*="captcha"]') ||
    /^(letter|cover_letter|resume|resume_id|captcha.*)$/i.test(el.name || '') ||
    (el.tagName === 'TEXTAREA' && /сопровод|письмо/i.test(el.getAttribute('placeholder') || '')) ||
    /^(username|current-password|new-password|one-time-code|cc-.*)$/i.test(el.autocomplete || '');
const fieldVisible = el => visible(el) || (
    /^(radio|checkbox)$/.test(el.type) && !el.closest('[hidden],[inert],[aria-hidden="true"]') &&
    [...(el.labels || [])].some(visible));
const controls = root => [...root.querySelectorAll('input,textarea,select')].filter(el =>
    !el.disabled && !el.readOnly && !excluded(el) &&
    /^(text|textarea|email|tel|url|number|radio|checkbox|select-one)$/.test(el.type) && fieldVisible(el));
const sources = el => {
    const ids = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
    if (ids.length) {
        const nodes = ids.map(id => document.getElementById(id));
        if (nodes.every(visible)) return nodes.map(node => ({node, attr: ''}));
        return [];
    }
    if (clean(el.getAttribute('aria-label'))) return [{node: el, attr: 'aria-label'}];
    if (el.tagName === 'FIELDSET') {
        const legend = el.querySelector(':scope > legend');
        if (visible(legend)) return [{node: legend, attr: ''}];
    }
    return [...(el.labels || [])].filter(visible).map(node => ({node, attr: ''}));
};
const read = refs => clean(refs.map(ref => ref.attr ? ref.node.getAttribute(ref.attr) : ref.node.innerText).join(' '));
const records = [], used = new Set();
for (const root of new Set(roots.filter(Boolean))) {
    if (!visible(root)) continue;
    const fields = controls(root);
    for (const field of fields) if (known.some(block => block.contains(field))) used.add(field);
    const add = (members, refs) => {
        const text = read(refs);
        if (!members.length || members.some(el => used.has(el)) || text.length < 8 || text.length > 4000) return;
        const types = new Set(members.map(el => el.type));
        if (members.length > 1 && (types.size !== 1 || !/^(radio|checkbox)$/.test(members[0].type))) return;
        if (members[0].type === 'radio' && new Set(members.map(el => el.name)).size !== 1) return;
        records.push({fields: members, sources: refs, text});
        members.forEach(el => used.add(el));
    };
    // Explicit groups keep choices together; nested groups belong to their own prompt.
    for (const group of root.querySelectorAll('fieldset,[role="group"],[role="radiogroup"]')) {
        if (group.querySelector('fieldset,[role="group"],[role="radiogroup"]')) continue;
        const members = fields.filter(el => group.contains(el));
        const refs = sources(group);
        if (refs.length) add(members, refs);
    }
    for (const field of fields) {
        if (used.has(field)) continue;
        if (/^(radio|checkbox)$/.test(field.type)) {
            // A choice label is an answer, never the question itself.
            const members = fields.filter(el => el.type === field.type && field.name && el.name === field.name);
            const refs = sources(field).filter(ref => !(field.labels || []).length ||
                ![...field.labels].includes(ref.node));
            if (members.length > 1 && refs.length && members.every(el => read(sources(el)) === read(refs))) {
                add(members, refs);
                continue;
            }
        } else {
            const refs = sources(field);
            if (refs.length) { add([field], refs); continue; }
        }
        // Ordinary wrappers with a heading or a direct text prompt, without HH-specific classes.
        for (let parent = field.parentElement, depth = 0; parent && parent !== root && depth < 5;
             parent = parent.parentElement, depth++) {
            if (!visible(parent) || parent.tagName === 'LABEL') continue;
            const members = fields.filter(el => parent.contains(el));
            const choices = members.filter(el => /^(radio|checkbox)$/.test(el.type));
            if (choices.length && (choices.length !== members.length || choices.length < 2 ||
                new Set(choices.map(el => el.type + ':' + el.name)).size !== 1)) continue;
            if (!choices.length && members.length !== 1) continue;
            const heading = parent.querySelector(':scope > h1,:scope > h2,:scope > h3,:scope > h4,:scope > legend');
            if (visible(heading)) { add(members, [{node: heading, attr: ''}]); break; }
            const direct = clean([...parent.childNodes].filter(n => n.nodeType === Node.TEXT_NODE).map(n => n.textContent).join(' '));
            if (direct.length >= 8) { add(members, [{node: parent, attr: ''}]); break; }
        }
    }
    const remaining = fields.filter(el => !used.has(el));
    if (remaining.length) {
        records.push({fields: remaining, sources: [], text: 'Не удалось однозначно разобрать поля анкеты', unresolved: true});
        remaining.forEach(el => used.add(el));
    }
}
return records;
"""


class DOMQuestionBlock:
    """Expose a semantic group to the existing answer and memory pipeline."""

    def __init__(self, driver, record):
        self.driver, self.record = driver, record
        self.id = 'dom:' + record['fields'][0].id
        self.unresolved = record.get('unresolved', False)

    @property
    def text(self):
        current = self.driver.execute_script("""
            const r = arguments[0];
            if (r.fields.some(el => !el.isConnected) || r.sources.some(ref => !ref.node.isConnected)) return null;
            if (r.unresolved) return r.text;
            return r.sources.map(ref => ref.attr ? ref.node.getAttribute(ref.attr) : ref.node.innerText)
                .join(' ').replace(/\\s+/g, ' ').trim();
        """, self.record)
        if current != self.record['text']:
            raise StaleElementReferenceException('Question changed after DOM snapshot')
        return current

    def find_elements(self, by, selector):
        if by != By.CSS_SELECTOR:
            raise ValueError('Semantic question supports CSS field queries only')
        self.text  # Do not apply a cached answer after the question has changed.
        return self.driver.execute_script(
            'return arguments[0].filter(el => el.matches(arguments[1]));', self.record['fields'], selector)


def collect_dom_questions(driver, container, known):
    try:
        records = driver.execute_script(SNAPSHOT_SCRIPT, container, known)
        if not isinstance(records, list):
            return []
        blocks = [DOMQuestionBlock(driver, record) for record in records]
        if blocks:
            logging.info('Анкета: резервный DOM-разбор нашёл дополнительных вопросов: %s', len(blocks))
        return blocks
    except Exception as exc:
        logging.debug('Резервный DOM-разбор анкеты недоступен: %s', type(exc).__name__)
        return []
