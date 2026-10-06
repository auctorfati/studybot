# Английский. Модуль 1 — целевой текст и банк блоков 0–1

Демонстрационный банк: тестовые данные кода и пример формата. Персонаж вымышленный. Бот читает этот файл напрямую; новая версия загружается в бота документом.

## 1. Целевой текст

Короткий рассказ о себе, из которого нарезаны фразы блоков. Всё в настоящем времени, вариант языка американский.

### Ядро, 90 секунд

Hi! My name is Sam. I'm thirty-four years old. I'm a math teacher, a writer and a psychologist. I'm also a PhD student in psychology. I'm not a doctor.

I live in Moscow, in the city center. I'm from Torzhok. It's a small town in the Tver region. My gym is near my home. There are parks and cafes near my home too.

## 2. Формат банка

Одна таблица на блок, строка — единица. Столбцы строго в таком порядке: ID, Подсказка, Целевая фраза, Допустимые варианты, Грамматика, Звук, Связи. Прочерк означает пустое поле.

Допустимые варианты разделяются косой чертой с пробелами. Сокращённая и полная форма (I'm и I am) отдельными вариантами не нужны: код приравнивает их сам, они записаны только для читающего. Если ячейка начинается со слов «по смыслу», код не сравнивает ответ со списком, а сразу передаёт модели; такие единицы — сборка блока, в сетку интервалов не идут и используются в контрольной точке.

Многоточие в целевой фразе («What does "…" mean?») — место для любого слова: код принимает любое заполнение.

Грамматика: коды заметок (З1, З2 и так далее) и при необходимости пояснение после двоеточия. Код берёт только коды, пояснение — для читающего. «Готовый кусок» означает, что фраза даётся целиком, без заметки.

Связи: коды единиц, в которые эта фраза превращается заменой лица, отрицанием, вопросом или кратким ответом. Связь двусторонняя и записывается один раз, у меньшего номера. Вид превращения код определяет сам по целевой фразе: знак вопроса — вопрос, not или n't — отрицание, начало с Yes или No — краткий ответ, остальное — утверждение; смена местоимения — другое лицо. Задание строится так: английская фраза одной единицы и русская подсказка другой, ответ — целевая фраза другой. Связь утверждения с вопросом даёт и формат «вопрос к ответу». Единица без связей проходит ступень 2 без трансформаций: для неё это ступень повтора.

Текст для аудио совпадает с целевой фразой; если нужен другой, в столбце «Звук» ставится «аудио: …». Две скорости, американский вариант.

Заметки: заголовок вида «### З1 — название», дальше текст. Код показывает заметку после первого знакомства с фразой, которая на неё ссылается, и передаёт её модели при проверке.

## 3. Грамматические заметки блока 1

Блок 0 заметок не имеет: фразы берутся целиком.

### З1 — to be: утверждение и отрицание

В английском предложении глагол есть всегда, даже там, где по-русски его нет: «я учитель» звучит как I'm a teacher, буквально «я есть учитель». Этот глагол — to be, в настоящем времени у него три формы: am после I, is после he, she, it, are после you, we, they. Отрицание — not сразу после формы: I'm not, he isn't, we aren't. Главная ловушка: am, is, are ставятся, когда говоришь, кто ты, какой ты или где ты, а не что делаешь. «Я работаю» — I work, а не «I'm work».

Примеры: I'm a math teacher. He isn't a doctor.

### З2 — to be: вопрос и краткие ответы

Вопрос строится перестановкой: форма глагола выходит вперёд, перед подлежащим. You are a doctor → Are you a doctor? He is your client → Is he your client? Отвечают кратко, той же формой: Yes, I am. No, I'm not. Yes, he is. No, he isn't. В кратком ответе «да» сокращения нет: Yes, I am, а не «Yes, I'm». Вопросительное слово стоит перед формой глагола: What's your name? How old are you?

Примеры: Are you a student? — No, I'm not. Is he your client? — Yes, he is.

### З3 — артикль a/an перед профессией

Перед профессией или ролью в единственном числе ставится a: I'm a teacher, she's a psychologist. Русскому уху он кажется лишним, но без него фраза звучит неправильно. Перед гласным звуком a превращается в an: an engineer; выбор идёт по звуку, а не по букве, поэтому a PhD student — PhD звучит «пи-эйч-ди». При перечислении артикль повторяется перед каждой профессией. Во множественном числе артикля нет: we're colleagues.

Примеры: I'm a math teacher, a writer and a psychologist. We aren't doctors.

### З4 — сокращения на слух и на письме

В разговоре to be сливается с местоимением: I'm, you're, he's, she's, we're, they're. Отрицание тоже сокращается: isn't, aren't; для I такой формы нет, говорят I'm not. На слух I'm звучит как «айм» — это не aim и не просто I. На письме апостроф обязателен: «Im» — ошибка. Полная форма не ошибка, но в разговоре звучит медленно и с нажимом.

Примеры: They're my clients. She isn't a student.

### З5 — местоимения

Личные местоимения стоят на месте подлежащего: I, you, he, she, we, they. Притяжательные стоят перед существительным и отвечают на вопрос «чей»: my, your, his, her. He и his — о мужчине, she и her — о женщине, путать нельзя: клиентка — she, her name. You — и «ты», и «вы», форма одна; your — и «твой», и «ваш». Отдельного слова «свой» нет: my, his, her по лицу.

Примеры: He's my client. What's her name?

### З6 — числа до ста и возраст

Возраст говорят через to be: I'm thirty-four, буквально «я есть тридцать четыре». Years old можно добавить или опустить, но years без old не говорят: «I'm thirty-four years» — ошибка. Десятки кончаются на -ty с ударением в начале (thirty, forty), числа 13–19 — на -teen с ударением на teen (thirteen, fourteen): на слух их легко спутать. Составные числа пишутся через дефис: thirty-four, forty-five; forty — без u. Вопрос: How old are you? How old is he?

Примеры: I'm thirty-four years old. He's about forty.

## 4. Блок 0. Рабочие фразы — 25 единиц

Направление: русская подсказка на входе, английская фраза голосом на выходе. Грамматика не объясняется. Звук блока: первое знакомство с th.

| ID | Подсказка | Целевая фраза | Допустимые варианты | Грамматика | Звук | Связи |
|---|---|---|---|---|---|---|
| 0.01 | Повтори, пожалуйста | Can you say that again, please? | Say that again, please. / Please repeat. | — | th звонкий: that | — |
| 0.02 | Ещё раз, пожалуйста | One more time, please. | Once more, please. | — | — | — |
| 0.03 | Помедленнее, пожалуйста | Could you speak more slowly, please? | Can you speak slower, please? / Slower, please. | — | — | — |
| 0.04 | Что значит «…»? | What does "…" mean? | — | — | — | — |
| 0.05 | Как сказать «…» по-английски? | How do you say "…" in English? | — | — | — | — |
| 0.06 | Я не понимаю | I don't understand. | I do not understand. | — | — | 0.07 |
| 0.07 | Понял, понятно | I understand. | I see. / Got it. / OK, I understand. | — | — | — |
| 0.08 | Дай подумать | Let me think. | Let me think a moment. | — | th глухой: think | — |
| 0.09 | Это правильно? | Is this correct? | Is that right? / Is it correct? | — | th звонкий: this, that | — |
| 0.10 | Я не знаю | I don't know. | I do not know. | — | — | 0.23 |
| 0.11 | Я не уверен | I'm not sure. | I am not sure. | — | — | — |
| 0.12 | Извини, я не расслышал | Sorry, I didn't catch that. | Sorry, I didn't hear that. | — | th звонкий: that | — |
| 0.13 | Как это пишется? | How do you spell that? | How do you spell it? | — | th звонкий: that | — |
| 0.14 | Объясни, пожалуйста | Can you explain, please? | Could you explain, please? / Explain, please. | — | — | — |
| 0.15 | Приведи пример | Can you give me an example? | Give me an example, please. | — | — | — |
| 0.16 | Спасибо | Thank you. | Thanks. / Thanks a lot. | — | th глухой: thank | — |
| 0.17 | Секунду | Just a second. | One second. / Just a moment. | — | — | — |
| 0.18 | Хорошо, ладно | OK. | All right. / Fine. | — | — | — |
| 0.19 | Я готов | I'm ready. | I am ready. / Ready. | — | — | — |
| 0.20 | Давай начнём | Let's start. | Let's begin. | — | — | 0.21, 0.22 |
| 0.21 | Давай продолжим | Let's continue. | Let's go on. / Next, please. | — | — | — |
| 0.22 | Давай закончим на этом | Let's stop here. | Let's finish here. / That's enough for today. | — | th звонкий: that's | — |
| 0.23 | Я не знаю это слово | I don't know this word. | I do not know this word. | — | th звонкий: this | — |
| 0.24 | Скажи это по-русски, пожалуйста | Can you say it in Russian, please? | In Russian, please. | — | — | — |
| 0.25 | Хороший вопрос | Good question. | That's a good question. | — | th звонкий: that's | — |

## 5. Блок 1. Кто я — 45 единиц

Цель: представиться и расспросить собеседника, кто он. Три шага на каждую тему: о себе, отрицание, вопрос собеседнику. Звуки блока: оба th, звонкий (this, the, they, that) и глухой (think, three, thirty, thank).

| ID | Подсказка | Целевая фраза | Допустимые варианты | Грамматика | Звук | Связи |
|---|---|---|---|---|---|---|
| 1.01 | Поздоровайся и назови себя | Hi! My name is Sam. | Hello, my name is Sam. / Hi, I'm Sam. | З5 | — | 1.02, 1.36 |
| 1.02 | Спроси, как зовут собеседника | What's your name? | What is your name? | З2, З4, З5 | — | 1.35, 1.37 |
| 1.03 | Рад познакомиться | Nice to meet you. | Nice to meet you, too. | готовый кусок | — | — |
| 1.04 | Мне тридцать четыре лет | I'm thirty-four years old. | I'm thirty-four. / I am thirty-four years old. | З1, З6 | th глухой: thirty | 1.05, 1.06, 1.33 |
| 1.05 | Мне тридцать четыре, скоро тридцать пять | I'm thirty-four, almost thirty-five. | I'm thirty-four. I'm almost thirty-five. | З6 | th глухой | — |
| 1.06 | Спроси о возрасте собеседника | How old are you? | — | З2 | — | 1.34 |
| 1.07 | Я учитель математики | I'm a math teacher. | I am a math teacher. | З1, З3 | — | 1.10, 1.15, 1.20, 1.21 |
| 1.08 | Я писатель | I'm a writer. | I am a writer. | З1, З3 | — | 1.10 |
| 1.09 | Я психолог | I'm a psychologist. | I am a psychologist. | З1, З3 | — | 1.10, 1.30 |
| 1.10 | Я учитель, писатель и психолог | I'm a math teacher, a writer and a psychologist. | I'm a teacher, a writer and a psychologist. | З3: артикль перед каждой | — | — |
| 1.11 | Я аспирант | I'm a PhD student. | I'm a graduate student. / I'm a postgraduate student. | З3 | — | 1.12, 1.14 |
| 1.12 | Я аспирант по психологии | I'm a PhD student in psychology. | I'm a graduate student in psychology. | З3 | — | — |
| 1.13 | Я не врач | I'm not a doctor. | I am not a doctor. | З1, З3 | — | 1.16, 1.25, 1.43 |
| 1.14 | Я не студент, я аспирант | I'm not a student. I'm a PhD student. | I'm not a student, I'm a PhD student. | З1 | — | 1.17, 1.26 |
| 1.15 | Спроси, кто собеседник по профессии | What do you do? | What's your job? | готовый кусок, разбор в блоке 3 | — | — |
| 1.16 | Ты врач? | Are you a doctor? | — | З2, З3 | — | — |
| 1.17 | Ты студент? | Are you a student? | — | З2 | — | 1.18, 1.19, 1.26, 1.44 |
| 1.18 | Ответь «да» на вопрос «ты студент?» | Yes, I am. | Yes, I'm a student. | З2 | — | — |
| 1.19 | Ответь «нет» на вопрос «ты студент?» | No, I'm not. | No, I'm not a student. | З2 | — | — |
| 1.20 | Ты учитель? | Are you a teacher? | Are you an instructor? | З2, З3 | — | 1.21, 1.24 |
| 1.21 | Я тоже учитель | I'm a teacher too. | I'm also a teacher. | З1 | — | — |
| 1.22 | Он мой клиент | He's my client. | He is my client. | З1, З4, З5 | — | 1.23, 1.27, 1.42 |
| 1.23 | Она моя клиентка | She's my client. | She is my client. | З1, З4, З5 | — | — |
| 1.24 | Он учитель | He's a teacher. | He is a teacher. | З1, З3 | — | — |
| 1.25 | Он не врач | He isn't a doctor. | He's not a doctor. / He is not a doctor. | З1, З4 | — | 1.43 |
| 1.26 | Она не студентка | She isn't a student. | She's not a student. | З1, З4 | — | 1.44 |
| 1.27 | Он твой клиент? | Is he your client? | — | З2, З5 | — | 1.28, 1.29 |
| 1.28 | Ответь «да» на вопрос «он твой клиент?» | Yes, he is. | Yes, he's my client. | З2 | — | — |
| 1.29 | Ответь «нет» на вопрос «он твой клиент?» | No, he isn't. | No, he's not. / No, he isn't my client. | З2 | — | — |
| 1.30 | Она психолог? | Is she a psychologist? | — | З2, З3 | — | — |
| 1.31 | Ему за тридцать | He's over thirty. | He is over thirty. | З6 | th глухой: thirty | 1.32, 1.34 |
| 1.32 | Ему около сорока | He's about forty. | He is about forty years old. | З6 | — | — |
| 1.33 | Ей тридцать два | She's thirty-two. | She is thirty-two years old. | З6 | th глухой | — |
| 1.34 | Сколько ему лет? | How old is he? | — | З2 | — | — |
| 1.35 | Как его зовут? | What's his name? | What is his name? | З5 | — | 1.36, 1.37 |
| 1.36 | Его зовут Иван | His name is Ivan. | He's Ivan. | З5 | — | — |
| 1.37 | Как её зовут? | What's her name? | What is her name? | З5 | — | — |
| 1.38 | Это мой друг | This is my friend. | — | З5 | th звонкий: this | 1.39, 1.40 |
| 1.39 | Это мой коллега | This is my colleague. | — | З5 | th звонкий | — |
| 1.40 | Кто это? | Who's this? | Who is this? / Who is that? | З2 | th звонкий | — |
| 1.41 | Мы коллеги | We're colleagues. | We are colleagues. | З1, З4, З5 | — | 1.43 |
| 1.42 | Они мои клиенты | They're my clients. | They are my clients. | З1, З4, З5 | th звонкий: they | 1.44 |
| 1.43 | Мы не врачи | We aren't doctors. | We're not doctors. / We are not doctors. | З1, З4 | — | — |
| 1.44 | Они не студенты | They aren't students. | They're not students. | З1, З4 | th звонкий | — |
| 1.45 | Мини-монолог: представься, назови возраст и три профессии, скажи, что ты не врач | Hi, I'm Sam. I'm thirty-four. I'm a math teacher, a writer and a psychologist. I'm also a PhD student. I'm not a doctor. | по смыслу: ядро блока | сборка блока | оба th | — |
