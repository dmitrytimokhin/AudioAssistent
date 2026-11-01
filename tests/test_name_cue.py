from audio_assistant.client.domain import Segment, Transcript
from audio_assistant.client.speakers.name_cue import NameCueAttributor


def attribute(team, *texts):
    tr = Transcript([Segment(i, i + 1, t) for i, t in enumerate(texts)])
    return NameCueAttributor(team).attribute(tr).merge_turns().segments


def test_switches_speaker_on_name_at_sentence_start(team):
    segs = attribute(team, "Начинаем.", "Катя. Делала анализ.", "Паша, скоринг в процессе.")
    assert [(s.speaker, s.text) for s in segs] == [
        (None, "Начинаем."),
        ("Вертоградова Екатерина", "Делала анализ."),
        ("Зеленский Павел", "скоринг в процессе."),
    ]


def test_cue_inside_one_segment_and_filler_prefix(team):
    segs = attribute(team, "Всё раскатил. Дальше Денис. Правил json.")
    assert [s.speaker for s in segs] == [None, "Губриенко Денис"]
    assert segs[0].text == "Всё раскатил."


def test_name_in_the_middle_is_not_a_cue(team):
    segs = attribute(team, "Роман. Обсудил это с Пашей вчера.")
    assert len(segs) == 1 and segs[0].speaker == "Гончар Роман"


def test_declined_name_and_yo(team):
    segs = attribute(team, "Кате слово. Катю не слышно.")  # «Кате» -> метка
    assert segs[0].speaker == "Вертоградова Екатерина"


def test_two_sergeys_by_surname(team):
    segs = attribute(team, "Дронов. Собрал json.", "Тимофеев. Нечего добавить.")
    assert [s.speaker for s in segs] == ["Дронов Сергей", "Тимофеев Сергей"]


def test_common_word_is_not_a_name(team):
    segs = attribute(team, "Роман. Катим как есть.")
    assert len(segs) == 1 and segs[0].speaker == "Гончар Роман"


def test_names_ending_in_soft_sign_and_y(team):
    segs = attribute(team, "Равиль. Таргет.", "Алексей. Выгрузки.", "Алексею слово. Ещё.")
    assert [s.speaker for s in segs] == ["Абдуллин Равиль", "Агафонов Алексей"]


def test_real_recording_phrasing(team):
    """Дословный вывод Whisper по тестовой записи: передача слова оборотами и «спасибо, имя»."""
    segs = attribute(
        team,
        "Раз, два, три. Сегодня начнем Дейли. Начнем с Ромы. Я сегодня сделал то-то-то. "
        "Спасибо, Рома. Теперь переходим к Денису. Я сегодня провел эксперимент. Спасибо, Денис. Теперь Катя.",
        "я ищу задачу самозанятых вернее их идентификацию спасибо катя",
        "следующий паша я делаю предсказание перехода самозанятых в п",
        "спасибо паша проверь я заканчиваю прогноз спасибо",
    )
    assert [(s.speaker, s.text) for s in segs] == [
        (None, "Раз, два, три. Сегодня начнем Дейли."),
        ("Гончар Роман", "Я сегодня сделал то-то-то."),
        ("Губриенко Денис", "Я сегодня провел эксперимент."),
        ("Вертоградова Екатерина", "я ищу задачу самозанятых вернее их идентификацию"),
        ("Зеленский Павел", "я делаю предсказание перехода самозанятых в п"),
        (None, "проверь я заканчиваю прогноз спасибо"),   # после «спасибо, паша» говорит не Паша
    ]


def test_thanks_in_dative_does_not_close_turn(team):
    segs = attribute(team, "Роман. Спасибо Паше за помощь. Всё сделал.")
    assert {s.speaker for s in segs} == {"Гончар Роман"}


def test_cpu_whisper_phrasing_comma_and_split_segments(team):
    """Дословный вывод faster-whisper: «..., начнем с Ромы» после запятой и метка, разорванная границей сегмента."""
    segs = attribute(
        team,
        "Раз, два, три, четыре, пять, шесть. Сегодня начнем с Дейли, начнем с Ромы. Я сегодня сделал то-то-то, "
        "провел три эксперимента, получил такой результат. Спасибо, Рома. Теперь переходим к Денису. "
        "Я сегодня провел такой-то эксперимент, получил первый, второй, третий результат. Спасибо, Денис. Теперь Катя.",
        "я ищу задачу самозанятых а вернее их идентификацию спасибо катя следующий",
        "паша я делаю и п вернее предсказание перехода самозанятых в п спасибо паша",
        "равен я заканчиваю прогноз за компаний спасибо",
    )
    assert [(s.speaker, s.text[:18]) for s in segs] == [
        (None, "Раз, два, три, чет"),
        ("Гончар Роман", "Я сегодня сделал т"),
        ("Губриенко Денис", "Я сегодня провел та"[:18]),
        ("Вертоградова Екатерина", "я ищу задачу самоза"[:18]),
        ("Зеленский Павел", "я делаю и п вернее"),
        (None, "равен я заканчиваю"),
    ]
    assert segs[0].text.endswith("начнем с Дейли,")   # хвост перед «начнем с Ромы» остаётся у ведущего


def test_handoff_phrase_only_with_name_and_noise_safe(team):
    segs = attribute(team, "Роман. Мы идём к следующему релизу, слово за слово. Начнем с утра.")
    assert {s.speaker for s in segs} == {"Гончар Роман"}


def test_unfinished_last_segment_is_not_lost(team):
    segs = attribute(team, "Катя. Анализ готов", "и проверен")
    assert [(s.speaker, s.text) for s in segs] == [("Вертоградова Екатерина", "Анализ готов и проверен")]
