from transcriber.asr import build_prompt
from transcriber.quality import (
    filler_only,
    foreign,
    junk_reason,
    prompt_leak,
    prompt_vocabulary,
    repetitive,
    tokenize,
)

GLOSSARY = ["Owner", "Ромашка", "Омни", "CDP", "Feature store", "АБ-тест", "Толк",
            "Анна Петрова", "Пётр Воронов", "Мария Лебедева", "Иван Сидоров"]
VOCAB = prompt_vocabulary([], GLOSSARY)


def test_tokenize_lowercases_and_keeps_digits():
    assert tokenize("Привет, CDP-2 — это Ромашка!") == ["привет", "cdp", "2", "это", "ромашка"]


def test_vocabulary_covers_prompt_keywords_participants_and_glossary():
    assert {"участники", "термины", "owner", "ромашка", "сидоров", "cdp"} <= VOCAB
    assert "петрова" in prompt_vocabulary(["Анна Петрова"], [])
    # словарь строится из исходных списков, а не из обрезанного по 600 символам промпта
    long_vocab = prompt_vocabulary([], [f"термин{i}" for i in range(200)])
    assert "термин199" in long_vocab and len(build_prompt([], [f"термин{i}" for i in range(200)])) <= 600


def test_prompt_leak_catches_prompt_echoes_from_the_real_meeting():
    for text in ["Термины, Иван Сидоров.",
                 "Owner, Ромашка, Омни, CDP, Факт, Анна Петрова.",
                 "Участники Сидоров, Ромашка, Офлайн, Ромашка, Офлайн, Ромашка, Офлайн, Ромашка.",
                 "Иван Сидоров, Анна Петрова. Иван Сидоров, Анна Петрова."]:
        assert prompt_leak(text, VOCAB), text


def test_prompt_leak_spares_normal_speech_and_short_phrases():
    assert not prompt_leak("Давайте тогда это запланируем, начнём с 1 октября", VOCAB)
    assert not prompt_leak("В Ромашке мы считаем CDP главным приоритетом на квартал", VOCAB)
    assert not prompt_leak("Ромашка выкатил новый личный кабинет", VOCAB)
    assert not prompt_leak("", VOCAB)


def test_prompt_leak_catches_two_word_echoes():
    """Пара слов, оба из промпта, — это тоже пересказ промпта, а не реплика."""
    assert prompt_leak("Термина, Сидоров", VOCAB)
    assert prompt_leak("Ромашка Омни", VOCAB)
    assert not prompt_leak("Ромашка растёт", VOCAB)         # только одно слово из промпта
    assert not prompt_leak("Ромашка", VOCAB)                # одно слово — не трогаем


def test_repetitive_catches_consecutive_ngrams_and_low_variety():
    assert repetitive("Ромашка, Офлайн, Ромашка, Офлайн, Ромашка, Офлайн")
    assert repetitive("да да да")
    assert repetitive("так так так так вот так так так")      # уникальных слов < 35 %
    assert not repetitive("Давайте начнём с первого октября, тогда всё сойдётся")
    assert not repetitive("да да, поехали")


def test_foreign_drops_english_on_a_russian_call_but_keeps_latin_acronyms():
    assert foreign("News Sl coming up.")
    assert not foreign("Все, продолжаем. Sorry.")
    assert not foreign("Мы отдадим SQL из CDP")
    assert not foreign("ok fine", language="ru")             # меньше трёх слов
    assert not foreign("News Sl coming up.", language="en")


def test_filler_only_drops_pure_hesitation():
    assert filler_only("Аааа... Ааа...")
    assert filler_only("Ну")
    assert filler_only("Э-э, ммм")
    assert not filler_only("Ну да, конечно")
    assert not filler_only("")


def test_junk_reason_names_the_rule_and_passes_clean_text():
    assert junk_reason("Термины, Иван Сидоров.", VOCAB) == "prompt leak"
    assert junk_reason("Аааа... Ааа...", VOCAB) == "filler"
    assert junk_reason("News Sl coming up.", VOCAB) == "foreign"
    assert junk_reason("Хорошо, давай обсудим контракт", VOCAB) is None
    assert junk_reason("", VOCAB) is None


def test_repetition_must_dominate_the_text_to_count():
    from transcriber.quality import repetitive
    assert repetitive("Термины. Термины. Термины. Термины.")
    assert repetitive("Мы так подумали. Мы так подумали. Мы так подумали. Мы так подумали.")
    # живая речь с «да, да, да» внутри — не повтор-галлюцинация
    assert not repetitive("Никак. Да, да, да, я здесь. Тогда у меня вопрос к Веронике по поводу собеседований на этой неделе и заявок на команду.")
    assert not repetitive("А-а-а, всё. А, и ты тоже. Вот видимо он как раз подключился, давайте тогда начнём с первого вопроса повестки.")


def test_leaked_label_prefix_is_stripped_not_the_whole_line():
    from transcriber.quality import strip_leak_prefix
    assert strip_leak_prefix("Термина, Слава, я на всякий случай ссылку кидал.") == "Слава, я на всякий случай ссылку кидал."
    assert strip_leak_prefix("Терминатор Ивановича, М.М.: Ага. Таня, слушай, а можно посмотреть метрики?") == "Ага. Таня, слушай, а можно посмотреть метрики?"
    assert strip_leak_prefix("И потом уже… Терминатор Ивановича, М.М.: Давай.") == "И потом уже… Давай."
    assert strip_leak_prefix("Термины.") == ""
    assert strip_leak_prefix("Термины обсудим позже, сначала метрики.") == "Термины обсудим позже, сначала метрики."
    assert strip_leak_prefix("Обычная фраза.") == "Обычная фраза."


def test_vocabulary_names_the_configured_owner():
    from transcriber.quality import prompt_vocabulary
    assert {"анна", "петрова"} <= prompt_vocabulary([], [], owner="Анна Петрова")


def test_two_terms_inside_a_short_real_phrase_are_not_a_leak():
    assert not prompt_leak("Есть Ромашка Омни", VOCAB)          # два термина из трёх слов — речь
    assert prompt_leak("Ромашка Омни CDP Факт", VOCAB)          # три и больше подряд — утечка
