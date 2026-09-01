<script lang="ts">
	import { once } from '$lib/once';
	import { enhance } from '$app/forms';
	import Pill from '$lib/components/Pill.svelte';
	import { n, when } from '$lib/charts/format';

	let { data, form } = $props();

	const DECISION = { accepted: 'принято', borderline: 'пограничное', rejected: 'отклонено' };

	const canRun = $derived(Boolean(data.harvestSchedule?.id) && Boolean(data.health?.available));
</script>

<svelte:head><title>Сбор · Проксима</title></svelte:head>

<div class="spread">
	<h1>Что мы ищем</h1>
	{#if !data.health?.available}
		<span class="pill pill-bad">Prefect недоступен — запуск невозможен</span>
	{/if}
</div>

<!-- Прогон запускают отсюда: сюда приходят, когда сбор «ничего не находит»,
     и заставлять ради одной кнопки идти в «Запуски» — лишний шаг. -->
<section class="run">
	<div>
		<h2>Собрать сейчас</h2>
		<p class="muted note">
			Обход всех источников, отсев по терминам и глобальная оценка. Прогон общий: результат
			увидят все подписчики, персонализация считается позже.
		</p>
	</div>
	<div class="run-actions">
		<form method="POST" action="?/collect" use:once>
			<input type="hidden" name="id" value={data.harvestSchedule?.id ?? ''} />
			<input type="hidden" name="label" value="Сбор материалов" />
			<button type="submit" class="btn-primary" disabled={!canRun}>Собрать статьи</button>
		</form>
		<form method="POST" action="?/collect" use:once>
			<input type="hidden" name="id" value={data.harvestSchedule?.id ?? ''} />
			<input type="hidden" name="label" value="Сбор за 30 дней" />
			<!-- Именно days_back: lookback_hours включает запасной режим одного
			     открытого окна, который упирается в лимит выдачи источника и
			     молча теряет хвост. Кнопка с тем же названием на «Запусках»
			     давно шлёт days_back — эти две расходились. -->
			<input
				type="hidden"
				name="parameters"
				value={JSON.stringify({ days_back: 30, limit_per_source: 200 })}
			/>
			<button type="submit" disabled={!canRun}>Собрать за 30 дней</button>
		</form>
	</div>
	{#if form?.error}<p class="flash err" role="alert">{form.error}</p>{/if}
	{#if form?.started}
		<p class="flash ok" role="status">«{form.label}» — запуск поставлен в очередь Prefect.</p>
	{/if}
	{#if form?.aborted !== undefined}
		<p class="flash ok" role="status">
			Снято прогонов: {form.aborted}. Теперь сбор можно запускать.
		</p>
	{/if}

	{#if data.activeRun}
		<!-- Одновременно идёт только один сбор. Пока эта запись висит, кнопки
		     выше отвечают «сбор уже идёт», и без объяснения это выглядит как
		     поломка. -->
		<div class="flash active">
			<div>
				<strong>Сейчас идёт прогон</strong>
				<span class="muted small">
					начат {when(data.activeRun.started_at)} ({data.activeRun.trigger})
				</span>
				<p class="muted small">
					Второй сбор параллельно не запустится. Если прогон завис — процесс упал, не
					закрыв запись, — снимите его: сам он подберётся только по таймауту.
				</p>
			</div>
			<form method="POST" action="?/abort" use:once>
				<button type="submit" class="btn-danger">Снять прогон</button>
			</form>
		</div>
	{/if}
</section>

<div class="two">
	<section class="panel">
		<header>
			<h2>Проба гейта</h2>
			<span class="muted">без записи в базу</span>
		</header>
		<div class="body">
			<p class="muted note">
				Вставьте заголовок реальной статьи — увидите, какие термины сработали и какая группа не
				выполнилась. Это дешевле, чем узнать о промахе профиля через неделю по пустому дайджесту.
			</p>
			<form method="POST" action="?/probe" use:enhance class="probe">
				<label>
					Заголовок
					<input name="title" value={form?.title ?? ''} required />
				</label>
				<label>
					Аннотация
					<textarea name="abstract" rows="3"></textarea>
				</label>
				<label>
					Журнал
					<input name="venue" placeholder="Géotechnique" />
				</label>
				<button type="submit" class="btn-primary">Проверить</button>
			</form>

			{#if form?.probe}
				<div class="result">
					<div class="row">
						<Pill status={form.probe.decision} map={DECISION} />
						<span class="mono">score {form.probe.keyword_score}</span>
						<span class="muted">порог {form.probe.threshold}</span>
					</div>
					<p class="dim">{form.probe.reason}</p>
					{#if form.probe.blocked_by}
						<p class="muted">Заблокировано группой <code>{form.probe.blocked_by}</code>.</p>
					{/if}
					{#each Object.entries(form.probe.matched_terms ?? {}) as [group, terms]}
						<div class="hit">
							<b>{group}</b>
							<span class="muted">{(terms as string[]).join(', ')}</span>
						</div>
					{/each}
				</div>
			{/if}
			{#if form?.error}<p class="err">{form.error}</p>{/if}
		</div>
	</section>

	<section class="panel">
		<header><h2>Профиль</h2></header>
		<div class="body">
			{#if data.profile?.profile}
				<p class="mono small">{data.profile.profile.satisfy_expr}</p>
				<div class="table-scroll">
					<table>
						<thead><tr><th>Группа</th><th>Режим</th><th class="num">Терминов</th></tr></thead>
						<tbody>
							{#each data.profile.groups as group}
								<tr>
									<td>{group.name ?? group.key}</td>
									<td class="muted">{group.mode}{group.is_hard ? ' · жёсткая' : ''}</td>
									<td class="num">{n(group.terms)}</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
				{#if data.stats?.totals}
					<p class="muted small">
						Терминов {n(data.stats.totals.total)}, включено {n(data.stats.totals.enabled)},
						ни разу не сработали {n(data.stats.totals.dead)}.
					</p>
				{/if}
			{:else}
				<p class="empty">Профиль сбора не загружен.</p>
			{/if}
		</div>
	</section>
</div>

<div class="two">
	<section class="panel">
		<header>
			<h2>Курсоры источников</h2>
			<span class="muted">где остановился каждый</span>
		</header>
		{#if data.cursors.length}
			<div class="table-scroll">
				<table>
					<thead><tr><th>Источник</th><th>Водяной знак</th><th>Последний успех</th></tr></thead>
					<tbody>
						{#each data.cursors as cursor}
							<tr>
								<td>{cursor.source}</td>
								<td class="muted">{cursor.watermark ? when(cursor.watermark) : 'окно по умолчанию'}</td>
								<td class="muted">{when(cursor.last_success_at)}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{:else}
			<p class="empty">Сбор ещё не запускался — курсоров нет.</p>
		{/if}
	</section>

	<section class="panel">
		<header><h2>Почему отклоняли</h2><span class="muted">за 30 дней</span></header>
		{#if data.reasons.length}
			<div class="table-scroll">
				<table>
					<thead><tr><th>Причина</th><th class="num">Материалов</th></tr></thead>
					<tbody>
						{#each data.reasons as reason}
							<tr><td>{reason.reason}</td><td class="num">{n(reason.n)}</td></tr>
						{/each}
					</tbody>
				</table>
			</div>
		{:else}
			<p class="empty">Отказов пока нет.</p>
		{/if}
	</section>
</div>

<style>
	.two {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(420px, 1fr));
		gap: var(--gap);
	}

	.note,
	.small {
		font-size: 12.5px;
	}

	.note {
		margin: 0;
		max-width: 70ch;
	}

	.probe {
		display: grid;
		gap: 10px;
	}

	.probe button {
		justify-self: start;
	}

	.result {
		display: grid;
		gap: 8px;
		padding: 14px;
		border: 1px solid var(--border);
		border-radius: var(--r-card);
		background: var(--bg);
	}

	.hit {
		font-size: 12.5px;
		display: flex;
		gap: 8px;
	}

	.err {
		color: var(--critical);
	}

	.run {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		justify-content: space-between;
		gap: var(--gap);
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: var(--r-panel);
		padding: 16px 18px;
		margin-bottom: var(--gap);
	}

	.run h2 {
		font-size: 15px;
		margin: 0 0 4px;
	}

	.run .note {
		margin: 0;
		max-width: 72ch;
		font-size: 13px;
	}

	.run-actions {
		display: flex;
		gap: 8px;
		flex-wrap: wrap;
	}

	/* Ответ на нажатие — внутри той же панели: сообщение в сотне пикселей
	   от кнопки читается как относящееся к чему-то другому. */
	.flash {
		flex-basis: 100%;
		margin: 0;
		padding-top: 12px;
		border-top: 1px solid var(--border-soft);
		font-size: 13px;
	}

	.active {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--gap);
		flex-wrap: wrap;
	}

	.active p {
		margin: 4px 0 0;
		max-width: 68ch;
	}

	.active strong {
		display: block;
	}

	.ok {
		color: var(--good);
	}
</style>
