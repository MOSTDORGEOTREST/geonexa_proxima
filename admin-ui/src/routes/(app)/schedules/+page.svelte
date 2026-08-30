<script lang="ts">
	import { enhance } from '$app/forms';
	import Pill from '$lib/components/Pill.svelte';
	import { when } from '$lib/charts/format';
	import { RUN_STATE_LABELS, STAGES, byRecency, type FlowAction } from '$lib/flows';

	let { data, form } = $props();

	const rows = $derived(data.schedules ?? []);
	const byKey = $derived(new Map(rows.map((row: any) => [row.key, row])));

	/** Строки, разложенные по этапам конвейера, плюс всё незнакомое в конце. */
	const grouped = $derived(
		(() => {
			const known = new Set(STAGES.flatMap((stage) => stage.kinds));
			const groups = STAGES.map((stage) => ({
				stage,
				rows: rows.filter((row: any) => stage.kinds.includes(row.kind))
			})).filter((group) => group.rows.length);
			const rest = rows.filter((row: any) => !known.has(row.kind));
			return rest.length
				? [...groups, { stage: { id: 'other', title: 'Прочее', summary: '', kinds: [] }, rows: rest }]
				: groups;
		})()
	);

	const pending = $derived(rows.filter((row: any) => row.sync_pending).length);
	const recent = $derived(byRecency(data.runs ?? []).slice(0, 10));
	const available = $derived(Boolean(data.health?.available));

	/** Параметры расписания плюс уточнения кнопки: кнопка дополняет, а не заменяет. */
	function payload(action: FlowAction, row: any): string {
		const merged = { ...(row?.parameters ?? {}), ...(action.parameters ?? {}) };
		return action.parameters ? JSON.stringify(merged) : '{}';
	}
</script>

<svelte:head><title>Расписания · Проксима</title></svelte:head>

<div class="spread">
	<h1>Запуск и расписания</h1>
	<div class="row">
		{#if available}
			<span class="pill pill-good">
				Prefect на связи{data.health?.running ? ` · ${data.health.running} в работе` : ''}
			</span>
		{:else}
			<span class="pill pill-bad">Prefect недоступен</span>
		{/if}
	</div>
</div>

{#if form?.error}<p class="err" role="alert">{form.error}</p>{/if}
{#if form?.started}<p class="ok" role="status">«{form.label}» — запуск поставлен в очередь Prefect.</p>{/if}
{#if form?.resynced !== undefined}
	<p class="ok" role="status">
		Дослано расписаний: {form.resynced}{form.resyncFailed ? `, не удалось: ${form.resyncFailed}` : ''}.
	</p>
{/if}

<!-- Ручной запуск идёт первым: за этим сюда и заходят. Расписания —
     настройка, которую трогают редко, и она ниже. -->
<section class="stages">
	{#each STAGES as stage}
		<article class="stage">
			<header>
				<h2>{stage.title}</h2>
				<p class="muted small">{stage.summary}</p>
			</header>
			<ul class="buttons">
				{#each stage.actions as action}
					{@const row = byKey.get(action.key)}
					<li>
						<form method="POST" action="?/run" use:enhance>
							<input type="hidden" name="id" value={row?.id ?? ''} />
							<input type="hidden" name="label" value={action.label} />
							<input type="hidden" name="parameters" value={payload(action, row)} />
							<button
								type="submit"
								class:btn-primary={action.primary}
								disabled={!row || !available}
							>
								{action.label}
							</button>
						</form>
						<p class="hint">
							{row ? action.hint : `Расписание «${action.key}» отсутствует в базе`}
						</p>
					</li>
				{/each}
			</ul>
		</article>
	{/each}
</section>

{#if !available}
	<p class="muted note">
		Пока оркестратор не отвечает, кнопки запуска выключены: задание некуда поставить. Проверьте
		контейнер <code>prefect-server</code> и переменную <code>PREFECT_API_URL</code>.
	</p>
{/if}

<section class="panel">
	<header>
		<div>
			<h2>Расписания</h2>
			<p class="muted small">
				Источник намерения — эта таблица, источник исполнения — Prefect. Правка сначала пишется в
				базу и только потом уходит в оркестратор: если он недоступен, расписание не теряется, а
				помечается несинхронизированным и досылается позже.
			</p>
		</div>
		{#if pending}
			<form method="POST" action="?/resync" use:enhance>
				<button type="submit" disabled={!available}>Дослать {pending} в Prefect</button>
			</form>
		{/if}
	</header>

	<div class="table-scroll">
		<table>
			<thead>
				<tr>
					<th>Флоу</th>
					<th>Расписание</th>
					<th>Ближайшие запуски</th>
					<th>Состояние</th>
					<th></th>
				</tr>
			</thead>
			{#each grouped as group}
				<tbody>
					<tr class="group">
						<th colspan="5" scope="colgroup">{group.stage.title}</th>
					</tr>
					{#each group.rows as row}
						<tr>
							<td>
								<div>{row.name}</div>
								<div class="muted small">{row.description ?? row.key}</div>
							</td>
							<td>
								<form method="POST" action="?/cron" use:enhance class="cron">
									<input type="hidden" name="id" value={row.id} />
									<input
										name="cron"
										value={row.cron ?? ''}
										class="mono"
										size="14"
										aria-label="Cron для «{row.name}»"
									/>
									<button type="submit" title="Сохранить расписание">↵</button>
								</form>
							</td>
							<td class="muted small">
								{#if row.schedule?.next?.length}
									{#each row.schedule.next.slice(0, 2) as next}
										<div>{when(next)}</div>
									{/each}
								{:else if row.schedule?.valid === false}
									<span class="pill pill-bad">не разбирается</span>
								{:else}
									—
								{/if}
							</td>
							<td>
								<div class="states">
									{#if row.enabled}
										<span class="pill pill-good">включено</span>
									{:else}
										<span class="pill pill-mute">выключено</span>
									{/if}
									{#if row.sync_pending}
										<span
											class="pill pill-warn"
											title="Расписание записано в базу, но ещё не доехало до Prefect"
										>
											не синхронизировано
										</span>
									{/if}
								</div>
							</td>
							<td class="actions">
								<form method="POST" action="?/run" use:enhance>
									<input type="hidden" name="id" value={row.id} />
									<input type="hidden" name="label" value={row.name} />
									<button type="submit" class="btn-primary" disabled={!available}>Запустить</button>
								</form>
								<form method="POST" action="?/toggle" use:enhance>
									<input type="hidden" name="id" value={row.id} />
									<button type="submit">{row.enabled ? 'Выключить' : 'Включить'}</button>
								</form>
							</td>
						</tr>
					{/each}
				</tbody>
			{/each}
		</table>
	</div>
</section>

<section class="panel">
	<header>
		<h2>Последние прогоны</h2>
		<a href="/runs">все прогоны и логи →</a>
	</header>
	{#if data.runs.length}
		<div class="table-scroll">
			<table>
				<thead>
					<tr><th>Флоу</th><th>Состояние</th><th>Начат</th><th>Завершён</th><th></th></tr>
				</thead>
				<tbody>
					{#each recent as run}
						<tr>
							<td>{run.flow ?? run.name ?? '—'}</td>
							<td>
								<Pill status={String(run.state ?? '').toLowerCase()} map={RUN_STATE_LABELS} />
							</td>
							<td class="muted small">{when(run.started_at)}</td>
							<td class="muted small">{when(run.finished_at)}</td>
							<td class="actions">
								{#if run.source === 'mirror'}
									<span class="muted small">зеркало в БД</span>
								{:else}
									<a class="btn" href="/runs/{run.id}">Логи</a>
								{/if}
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	{:else}
		<p class="empty">Прогонов ещё не было.</p>
	{/if}
</section>

<style>
	.stages {
		display: grid;
		gap: var(--gap);
		grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
		margin-bottom: var(--gap);
	}

	.stage {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: var(--r-panel);
		padding: 16px 18px 18px;
		display: grid;
		gap: 14px;
		align-content: start;
	}

	.stage h2 {
		font-size: 15px;
		margin: 0 0 4px;
	}

	.stage header p {
		margin: 0;
		max-width: 46ch;
	}

	/* Подпись под кнопкой, а не рядом: в колонке карточки на подпись
	   остаётся сотня пикселей, и рядом она рвётся на семь строк. */
	.buttons {
		list-style: none;
		margin: 0;
		padding: 0;
		display: grid;
		gap: 12px;
	}

	.buttons li {
		display: grid;
		gap: 4px;
		justify-items: start;
	}

	.buttons button {
		text-align: left;
	}

	.hint {
		margin: 0;
		color: var(--muted);
		font-size: 12px;
		line-height: 1.35;
		/* Длинные имена переменных и ключей не должны распирать карточку. */
		overflow-wrap: anywhere;
	}

	.note {
		max-width: 78ch;
		font-size: 13px;
	}

	.small {
		font-size: 12px;
	}

	tr.group th {
		background: var(--surface-2);
		color: var(--text-dim);
		font-size: 11.5px;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		padding: 8px 12px;
		border-bottom: 1px solid var(--border);
	}

	.states {
		display: flex;
		flex-wrap: wrap;
		gap: 4px;
	}

	.cron {
		display: flex;
		gap: 4px;
	}

	.cron input {
		width: 130px;
	}

	.cron button {
		padding: 4px 10px;
	}

	.actions {
		display: flex;
		gap: 6px;
		justify-content: flex-end;
	}

	.actions button,
	.actions .btn {
		padding: 4px 12px;
		font-size: 12.5px;
	}

	.actions .btn {
		text-decoration: none;
		display: inline-block;
	}

	.err {
		color: var(--critical);
	}

	.ok {
		color: var(--good);
	}
</style>
