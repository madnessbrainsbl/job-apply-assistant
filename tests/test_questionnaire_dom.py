"""Semantic fallback on local forms in an isolated real Chrome profile."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from selenium import webdriver
from selenium.webdriver.common.by import By

from db_manager import DatabaseManager
from hh_selenium import HHSeleniumBot


@pytest.fixture(scope='module')
def browser(tmp_path_factory):
    options = webdriver.ChromeOptions()
    options.add_argument('--headless=new')
    options.add_argument(f'--user-data-dir={tmp_path_factory.mktemp("semantic-chrome")}')
    driver = webdriver.Chrome(options=options)
    try:
        yield driver
    finally:
        driver.quit()


def open_form(browser, tmp_path, html):
    page = tmp_path / 'form.html'
    page.write_text('<meta charset="utf-8">' + html, encoding='utf-8')
    browser.get(page.as_uri())
    bot = HHSeleniumBot.__new__(HHSeleniumBot)
    bot.driver, bot.config = browser, {'question_answers': {}}
    bot.db = DatabaseManager()
    bot.ai_assistant = None
    bot.click_element_with_mouse = lambda element: element.click() or True
    return bot


FORM = '''<form>
<label for="experience">Расскажите об опыте проверки API</label><textarea id="experience"></textarea>
<fieldset><legend>Какой формат работы рассматриваете?</legend>
  <label><input type="radio" name="format" value="office">Офис</label>
  <label><input type="radio" name="format" value="remote">Удалённо</label>
</fieldset>
<div role="group" aria-labelledby="languages"><h3 id="languages">Какие языки используете?</h3>
  <label><input type="checkbox" value="python">Python</label>
  <label><input type="checkbox" value="go">Go</label>
</div>
<div><h3>Сколько будет 1 плюс 2?</h3><input id="years" type="number"></div>
<span id="city-label">Какой город проживания?</span><select id="city" aria-labelledby="city-label">
  <option></option><option>Москва</option><option>Красноярск</option></select>
<button type="submit" data-qa="vacancy-response-submit-popup" onclick="window.sent=true;return false">Откликнуться</button>
</form>'''


def test_semantic_questions_fill_through_confirmed_memory(browser, tmp_path, monkeypatch):
    bot = open_form(browser, tmp_path, FORM)
    blocks = bot.collect_question_blocks()
    assert len(blocks) == 5
    answers = {'Расскажите об опыте проверки API': ['Проверял API с pytest.'],
               'Какой формат работы рассматриваете?': ['Удалённо'],
               'Какие языки используете?': ['Python', 'Go'],
               'Сколько будет 1 плюс 2?': ['3'],
               'Какой город проживания?': ['Красноярск']}
    for block in blocks:
        _, kind, options, _ = bot.describe_memory_question(block, block.text)
        key = bot.question_memory().stage(block.text, kind, options, answers[block.text])
        bot.question_memory().confirm(key)
    bot.ai_assistant = SimpleNamespace(enabled=True, answer_questions_batch=Mock(side_effect=AssertionError('No AI')))
    monkeypatch.setattr(browser, 'get_screenshot_as_png', Mock(side_effect=AssertionError('DOM needs no screenshot')))
    assert bot.answer_employer_questions() == 6
    assert not bot.unanswered_questions
    assert browser.find_element(By.ID, 'experience').get_attribute('value') == 'Проверял API с pytest.'
    assert browser.find_element(By.CSS_SELECTOR, 'input[value="remote"]').is_selected()
    assert all(el.is_selected() for el in browser.find_elements(By.CSS_SELECTOR, 'input[type="checkbox"]'))
    assert browser.find_element(By.ID, 'years').get_attribute('value') == '3'
    assert browser.find_element(By.ID, 'city').get_attribute('value') == 'Красноярск'
    assert not browser.execute_script('return !!window.sent')
    bot.ai_assistant.answer_questions_batch.assert_not_called()


def test_existing_and_unknown_blocks_are_not_duplicated(browser, tmp_path):
    bot = open_form(browser, tmp_path, FORM.replace('<label for="experience">',
        '<div data-qa="vacancy-response-popup-form-question"><label for="experience">').replace(
        '</textarea>', '</textarea></div>'))
    blocks = bot.collect_question_blocks()
    assert len(blocks) == 5
    assert not blocks[0].id.startswith('dom:')
    ids = [el.id for block in blocks for el in block.find_elements(By.CSS_SELECTOR, 'input,textarea,select')]
    assert len(ids) == len(set(ids)) == 7


@pytest.mark.parametrize('html', [
    '<form><label>Поиск вакансии<input></label></form>',
    '<form><label>Электронная почта<input type="email"></label><input type="password"></form>',
    '<div><label>Какой город проживания?<input></label></div>',
])
def test_unrelated_forms_are_not_scanned(browser, tmp_path, html):
    bot = open_form(browser, tmp_path, html)
    assert bot.collect_question_blocks() == []


def test_explicit_container_is_scoped_and_ignores_hidden_and_service_fields(browser, tmp_path):
    html = '''<label>Внешний вопрос<input id="outside"></label><div id="application">
    <label for="answer">Расскажите об опыте API</label><textarea id="answer"></textarea>
    <div hidden><label>Скрытый вопрос<textarea></textarea></label></div>
    <div style="height:0;overflow:hidden"><label>Обрезанный вопрос<textarea></textarea></label></div>
    <label>Сопроводительное письмо<textarea data-qa="vacancy-response-letter"></textarea></label>
    <textarea placeholder="Сопроводительное письмо"></textarea><select name="resume"><option>Моё резюме</option></select>
    <input type="password"><input type="file"><input type="hidden">
    <label>Недоступный вопрос<input disabled></label>
    </div>'''
    bot = open_form(browser, tmp_path, html)
    blocks = bot.collect_question_blocks(browser.find_element(By.ID, 'application'))
    assert len(blocks) == 1
    assert blocks[0].text == 'Расскажите об опыте API'


def test_ambiguous_fields_block_submission_and_do_not_reach_ai(browser, tmp_path):
    bot = open_form(browser, tmp_path, '''<form><fieldset><legend>Расскажите об опыте</legend>
    <input><input></fieldset><button data-qa="vacancy-response-submit-popup">Откликнуться</button></form>''')
    bot.ai_assistant = SimpleNamespace(enabled=True, answer_questions_batch=Mock())
    assert bot.answer_employer_questions() == 0
    assert bot.unanswered_questions == ['Не удалось однозначно разобрать поля анкеты']
    bot.ai_assistant.answer_questions_batch.assert_not_called()


def test_changed_question_is_not_filled_with_cached_answer(browser, tmp_path):
    bot = open_form(browser, tmp_path, FORM)
    block = next(block for block in bot.collect_question_blocks() if 'формат' in block.text)
    browser.execute_script('document.querySelector("legend").textContent="Готовы ли работать только в офисе?"')
    count, unresolved = bot.answer_single_question(block, 'Какой формат работы рассматриваете?', {}, set())
    assert count == 0 and 'не удалось разобрать' in unresolved
    assert not any(el.is_selected() for el in browser.find_elements(By.CSS_SELECTOR, 'input[type="radio"]'))


def test_hidden_native_radios_and_aria_option_labels(browser, tmp_path):
    bot = open_form(browser, tmp_path, '''<form><fieldset><legend>Какой формат работы рассматриваете?</legend>
    <label><input style="opacity:0;position:absolute;width:1px;height:1px" type="radio" name="format" value="office">Офис</label>
    <label><input style="opacity:0;position:absolute;width:1px;height:1px" type="radio" name="format" value="remote">Удалённо</label>
    </fieldset><fieldset><legend>Какие языки используете?</legend>
    <input type="checkbox" aria-label="Python"><input type="checkbox" aria-labelledby="go"><span id="go">Go</span>
    </fieldset><button data-qa="vacancy-response-submit-popup">Откликнуться</button></form>''')
    blocks = bot.collect_question_blocks()
    assert len(blocks) == 2
    assert bot.describe_memory_question(blocks[0], blocks[0].text)[2] == ['Офис', 'Удалённо']
    assert bot.describe_memory_question(blocks[1], blocks[1].text)[2] == ['Python', 'Go']


def test_dom_error_keeps_standard_parser_working(browser, tmp_path, monkeypatch):
    from questionnaire_dom import SNAPSHOT_SCRIPT
    bot = open_form(browser, tmp_path, '<div data-qa="task-body">Расскажите об опыте<textarea></textarea></div>')
    execute = browser.execute_script
    def fail_snapshot(script, *args):
        if script == SNAPSHOT_SCRIPT:
            raise RuntimeError('DOM unavailable')
        return execute(script, *args)
    monkeypatch.setattr(browser, 'execute_script', fail_snapshot)
    assert len(bot.collect_question_blocks()) == 1
