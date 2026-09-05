<script lang="ts">
	import { enhance } from '$app/forms';
	import Pill from '$lib/components/Pill.svelte';
	import { n, when } from '$lib/charts/format';

	let { data, form } = $props();

	const KIND = { user: 'личный чат', group: 'группа', channel: 'канал' } as const;
	const STATUS = {
		active: 'активен',
		pending: 'ждёт подтверждения',
		paused: 'пауза',
		blocked: 'заблокирован',
		left: 'ушёл'
	};

	const subscriber = $derived(data.card?.subscriber ?? {});
	const profile = $derived(data.active);
	const explicit = $derived(data.interests?.explicit ?? []);
	const learned = $derived(data.interests?.learned ?? []);
	const events = $derived(data.activity?.items ?? []);
	const isChat = $derived(subscriber.kind !== 'user');

	/**
	 * Живой разбор описания: на какие темы оно распадётся и что не сработает.
	 *
	 * Разбиение механическое и из поля ввода не видно — одно и то же
	 * предложение может стать одной темой или двумя. Показанный результат
	 * заменяет собой инструкцию: ошибку видно до сохранения, а не через месяц
	 * не того дайджеста.
	 */
	let draft = $state<string | null>(null);
	let live = $state<{ facets?: any[]; dropped?: string[]; notes?: any[] } | null>(null);
	let previewError = $state<string | null>(null);
	let checking = $state(false);
	let timer: ReturnType<typeof setTimeout> | undefined;
	// Номер запроса: ответы приходят не в том порядке, в каком уходили, и
	// поздний ответ на старый текст затирал бы разбор нового.
	let issued = 0;

	// Пока черновик не трогали, показываем разбор сохранённого профиля: он
	// учитывает ещё и явные темы, которых в поле описания нет.
	const shown = $derived(draft === null || live === null ? (data.review ?? null) : live);
	const notes = $derived(shown?.notes ?? []);
	const facets = $derived(shown?.facets ?? []);
	const dropped = $derived(shown?.dropped ?? []);

	function onDescription(event: Event): void {
		draft = (event.currentTarget as HTMLTextAreaElement).value;
		clearTimeout(timer);
		// Пауза, а не запрос на каждую букву: считает Python на той стороне,
		// и молотить его на каждом нажатии незачем.
		timer = setTimeout(check, 400);
	}

	// Таймер живёт дольше страницы, если уйти с неё во время паузы.
	$effect(() => () => clearTimeout(timer));

	async function check(): Promise<void> {
		if (draft === null) return;
		const ticket = ++issued;
		checking = true;
		try {
			const response = await fetch(`/subscribers/${subscriber.id}/preview`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ description: draft, profile_id: profile?.id ?? null })
			});
			const payload = await response.json();
			if (ticket !== issued) return;
			if (!response.ok) {
				// Молчаливый провал здесь опаснее всего: экран, который
				// существует ради поиска ошибок в профиле, показывал бы
				// «замечаний нет» при недоступном API.
				previewError = String(payload?.error ?? 'проверка недоступна');
				live = null;
			} else {
				previewError = null;
				live = payload;
			}
		} catch (error) {
			if (ticket !== issued) return;
			previewError = (error as Error).message || 'проверка недоступна';
			live = null;
		} finally {
			if (ticket === issued) checking = false;
		}
	}

	const LEVEL: Record<string, string> = {
		error: 'не сработает',
		warning: 'сработает не так',
		hint: 'можно лучше'
	};
</script>

<svelte:head>
	<title>{subscriber.title || subscriber.telegram_chat_id} · Проксима</title>
</svelte:head>

<div class="spread">
	<div>
		<h1>{subscriber.title || subscriber.telegram_username || subscriber.telegram_chat_id}</h1>
		<p class="muted small">
			{KIND[subscriber.kind as keyof typeof KIND] ?? subscriber.kind} ·
			<span class="mono">{subscriber.telegram_chat_id}</span>
			{#if subscriber.bot_status} · бот: {subscriber.bot_status}{/if}
		</p>
	</div>
	<div class="row">
		<Pill status={subscriber.status} map={STATUS} />
		{#if subscriber.status !== 'active'}
			<form method="POST" action="?/approve" use:enhance>
				<button type="submit" class="btn-primary">Подтвердить</button>
			</form>
		{:else}
			<form method="POST" action="?/block" use:enhance>
				<button type="submit" class="btn-danger">Заблокировать</button>
			</form>
		{/if}
		<a class="btn" href="/subscribers">К списку</a>
	</div>
</div>

{#if form?.error}<p class="err" role="alert">{form.error}</p>{/if}

{#if isChat}
	<p class="muted note">
		Профиль интересов чата ведёт администратор: участники его не правят. Всё, что записано
		здесь, попадает в <code>compiled_text</code>, по которому отбираются материалы, — описание
		и темы пересобираются сразу при сохранении.
	</p>
{/if}

{#if profile}
	<section class="panel">
		<header>
			<h2>Профиль · {profile.name}</h2>
			<p class="muted small">версия {n(profile.version)}</p>
		</header>

		<form method="POST" action="?/profile" use:enhance class="editor">
			<input type="hidden" name="profile_id" value={profile.id} />
			<label for="description">
				<span class="label-row">
					Описание интересов обычными словами
					<span class="hint">— одна область на предложение или строку</span>
				</span>
			</label>
			<textarea
				id="description"
				name="description"
				rows="5"
				oninput={onDescription}
				placeholder={'Математическое моделирование в геотехнике: МКЭ и определяющие соотношения грунтов.\nРазжижение грунтов при циклических нагрузках.\nИИ для обработки данных полевых и лабораторных опытов.'}
				>{profile.description ?? ''}</textarea
			>
			<!-- Английская сторона профиля: её делает LLM при сохранении, и по
			     ней идёт поиск в англоязычном корпусе. Показываем, а не даём
			     править: правка русского описания перезапишет перевод. -->
			<div class="english">
				<span class="label-row">
					<span class="muted small caps">Английская сторона</span>
					<span class="hint">— перевод для поиска, обновляется при сохранении</span>
				</span>
				{#if profile.description_en}
					<p class="dim small en">{profile.description_en}</p>
				{:else if profile.description}
					<p class="muted small">
						Перевода пока нет — модель была недоступна при сохранении. Сохраните описание ещё раз.
					</p>
				{:else}
					<p class="muted small">Появится вместе с описанием.</p>
				{/if}
			</div>

			<div class="split">
				<div>
					<h3 class="split-title">
						<span>Темы, по которым пойдёт поиск</span>
						{#if checking}<span class="counting">считаю…</span>{/if}
					</h3>
					{#if facets.length}
						<ol class="facets">
							{#each facets as facet}
								<li>
									{facet.text}
									{#if facet.source === 'interest'}<span class="muted">· из тем</span>{/if}
									{#if facet.source === 'description_en'}<span class="muted">· перевод</span>{/if}
								</li>
							{/each}
						</ol>
					{:else}
						<p class="muted small">
							Отдельных тем нет — поиск пойдёт только по профилю целиком.
						</p>
					{/if}
					{#if dropped.length}
						<p class="dropped muted small">
							Не стало отдельной темой (слишком коротко, приклеилось к соседней):
							{dropped.join(' · ')}
						</p>
					{/if}
				</div>

				<div class="checks">
					<h3 class="split-title"><span>Проверка</span></h3>
					{#if previewError}
						<p class="err small">Проверка не отработала: {previewError}. Разбор ниже — от
							последнего сохранения.</p>
					{/if}
					{#if notes.length}
						<ul class="notes">
							{#each notes as note}
								<li class={note.level}>
									<b>{LEVEL[note.level] ?? note.level}:</b>
									{note.text}
								</li>
							{/each}
						</ul>
					{:else}
						<p class="muted small">Замечаний нет.</p>
					{/if}
					<p class="small"><a href="/guide">Как писать профиль →</a></p>
				</div>
			</div>

			<div class="settings">
				<label>
					Формат
					<select name="delivery_format" value={profile.delivery_format}>
						<option value="cards">карточки</option>
						<option value="compact">компактно</option>
						<option value="single_message">одним сообщением</option>
						<option value="digest_post">пост в канал</option>
					</select>
				</label>
				<label>
					Материалов
					<input name="max_items" type="number" min="1" max="100" value={profile.max_items} />
				</label>
				<label>
					Порог
					<input
						name="min_personal_score"
						type="number"
						min="0"
						max="1"
						step="0.05"
						value={profile.min_personal_score}
					/>
				</label>
				<label class="check">
					<input type="checkbox" name="digest_enabled" checked={profile.digest_enabled} />
					Плановый дайджест
				</label>
			</div>
			<div class="commit">
				<button type="submit" class="btn-primary">Сохранить профиль</button>
				<span class="muted small">
					Следующий дайджест: {when(profile.next_digest_at) || 'не назначен'}
				</span>
			</div>
		</form>
	</section>

	<section class="panel">
		<header>
			<h2>Темы</h2>
			<p class="muted small">
				Вес 0-10, сравнивает темы между собой. Минус убирает тему из выдачи. Пишите оба
				написания через «;» — тема сверяется с текстом статей буквально, а он английский.
			</p>
		</header>

		{#if explicit.length}
			<div class="table-scroll">
				<table>
					<thead><tr><th>Тема</th><th>Знак</th><th class="num">Вес</th><th></th></tr></thead>
					<tbody>
						{#each explicit as row (row.id)}
							<tr>
								<td>{row.topic || row.query}</td>
								<td class="muted">{row.polarity === 'positive' ? 'плюс' : 'минус'}</td>
								<td class="num">{row.weight}</td>
								<td class="actions">
									<form method="POST" action="?/drop_interest" use:enhance>
										<input type="hidden" name="profile_id" value={profile.id} />
										<input type="hidden" name="interest_id" value={row.id} />
										<button type="submit">Убрать</button>
									</form>
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{:else}
			<p class="empty">Явных тем нет — работает только описание профиля.</p>
		{/if}

		<form method="POST" action="?/interest" use:enhance class="add">
			<input type="hidden" name="profile_id" value={profile.id} />
			<input name="query" placeholder="liquefaction; разжижение грунтов" />
			<select name="polarity">
				<option value="positive">плюс</option>
				<option value="negative">минус</option>
			</select>
			<input name="weight" type="number" min="0" max="10" step="0.5" value="5" />
			<button type="submit">Добавить</button>
		</form>

		{#if learned.length}
			<details>
				<summary class="muted">Выученное по реакциям ({n(learned.length)})</summary>
				<ul class="learned">
					{#each learned as row (row.id)}
						<li>
							<span class="muted">{row.polarity === 'positive' ? '+' : '−'}{row.weight}</span>
							{row.query}
							<span class="muted small">· подтверждений: {n(row.evidence_count)}</span>
						</li>
					{/each}
				</ul>
			</details>
		{/if}
	</section>
{:else}
	<section class="panel"><p class="empty">У подписчика нет профиля.</p></section>
{/if}

<section class="panel">
	<header><h2>Сообщение в чат</h2></header>
	<form method="POST" action="?/message" use:enhance class="send">
		<input name="text" placeholder="Отправить сообщение мимо очереди дайджестов" />
		<button type="submit">Отправить</button>
	</form>
</section>

{#if events.length}
	<section class="panel">
		<header><h2>Активность</h2></header>
		<div class="table-scroll">
			<table>
				<thead><tr><th>Событие</th><th>Когда</th></tr></thead>
				<tbody>
					{#each events as row}
						<tr><td class="mono">{row.kind}</td><td class="muted">{when(row.occurred_at)}</td></tr>
					{/each}
				</tbody>
			</table>
		</div>
	</section>
{/if}

<style>
	.english {
		display: grid;
		gap: 4px;
		margin: 6px 0 10px;
	}

	.caps {
		text-transform: uppercase;
		letter-spacing: 0.03em;
	}

	.en {
		margin: 0;
		white-space: pre-line;
		line-height: 1.5;
		max-width: 90ch;
	}

	.note {
		max-width: 78ch;
		font-size: 13px;
		margin: 0;
	}

	/* Редактор — три смысловых блока: текст, его разбор, настройки доставки.
	   Между блоками воздуха больше, чем внутри блока, иначе экран читается
	   одной простынёй и глазу не за что зацепиться. */
	.editor {
		display: grid;
		gap: 18px;
		padding: 16px 18px 18px;
	}

	/* Подпись принадлежит полю под ней, а не промежутку между блоками. */
	.editor > label {
		margin-bottom: -9px;
	}

	textarea {
		min-height: 210px;
	}

	.split {
		display: grid;
		grid-template-columns: minmax(0, 1.45fr) minmax(240px, 1fr);
		gap: 24px;
		padding: 16px 18px 18px;
		border: 1px solid var(--border);
		border-radius: var(--r-field);
		background: var(--surface-2);
	}

	/* Правая половина — вывод о тексте, левая — сам разбор. Разделитель
	   вместо пустоты: без него короткая колонка выглядит обрезанной. */
	.checks {
		display: flex;
		flex-direction: column;
		padding-left: 24px;
		border-left: 1px solid var(--border);
	}

	/* Ссылка на инструкцию прижата к низу колонки: замечаний обычно меньше,
	   чем тем, и без этого правая половина выглядит оборванной. */
	.checks > .small:last-child {
		margin: auto 0 0;
		padding-top: 14px;
	}

	.split-title {
		display: flex;
		align-items: baseline;
		gap: 8px;
		margin: 0 0 10px;
		font-family: var(--font-s);
		font-size: 12px;
		font-weight: 500;
		letter-spacing: 0.05em;
		text-transform: uppercase;
		color: var(--muted);
	}

	.counting {
		letter-spacing: 0;
		text-transform: none;
		font-size: 12.5px;
	}

	.facets,
	.notes {
		margin: 0;
		padding-left: 20px;
		font-size: 13px;
		line-height: 1.5;
	}

	.facets li,
	.notes li {
		margin-bottom: 7px;
	}

	.facets li::marker {
		color: var(--muted);
	}

	.facets li:last-child,
	.notes li:last-child {
		margin-bottom: 0;
	}

	/* Отсеянное — сноска к списку, а не его продолжение. */
	.dropped {
		margin: 12px 0 0;
		padding-top: 10px;
		border-top: 1px dashed var(--border);
	}

	.settings {
		display: grid;
		grid-template-columns: minmax(180px, 1.2fr) minmax(110px, 0.7fr) minmax(110px, 0.7fr) auto;
		align-items: end;
		gap: var(--gap);
	}

	/* Флажок держит высоту соседних полей и выравнивается с ними по низу:
	   иначе он висит в воздухе посреди строки, а подпись уезжает в две
	   строки капса. */
	.check {
		display: flex;
		align-items: center;
		gap: 9px;
		height: 40px;
		padding: 0 14px;
		border: 1px solid var(--border);
		border-radius: var(--r-field);
		color: var(--text);
		font-size: 13px;
		letter-spacing: 0;
		text-transform: none;
		white-space: nowrap;
		cursor: pointer;
	}

	.commit {
		display: flex;
		align-items: center;
		justify-content: space-between;
		flex-wrap: wrap;
		gap: 12px;
		padding-top: 16px;
		border-top: 1px solid var(--border-soft);
	}

	/* Добавление темы: поле запроса тянется, знак и вес — фиксированные,
	   кнопка по содержимому. */
	.add {
		display: grid;
		grid-template-columns: minmax(0, 1fr) 150px 110px auto;
		align-items: center;
		gap: 10px;
		padding: 16px 18px 18px;
		border-top: 1px solid var(--border-soft);
	}

	/* Отдельная форма: с сеткой формы тем кнопка «Отправить» раздувалась до
	   ширины колонки веса. */
	.send {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		gap: 10px;
		padding: 16px 18px 18px;
	}

	details {
		padding: 0 18px 18px;
	}

	summary {
		cursor: pointer;
		font-size: 13px;
	}

	.learned {
		display: grid;
		gap: 4px;
		margin: 10px 0 0;
		padding-left: 18px;
		font-size: 13px;
	}

	.actions {
		text-align: right;
	}

	.actions button {
		padding: 4px 12px;
		font-size: 12.5px;
	}

	.small {
		font-size: 12.5px;
	}

	.notes li.error {
		color: var(--critical);
	}

	.notes li.warning {
		color: var(--warning);
	}

	.err {
		color: var(--critical);
	}

	@media (max-width: 1180px) {
		.settings {
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}

		.settings .check {
			grid-column: span 2;
			justify-self: start;
		}
	}

	@media (max-width: 900px) {
		.split {
			grid-template-columns: 1fr;
			gap: 18px;
		}

		.checks {
			padding-left: 0;
			padding-top: 16px;
			border-left: none;
			border-top: 1px solid var(--border);
		}

		.settings,
		.add,
		.send {
			grid-template-columns: 1fr;
		}

		.settings .check {
			grid-column: auto;
		}
	}
</style>
