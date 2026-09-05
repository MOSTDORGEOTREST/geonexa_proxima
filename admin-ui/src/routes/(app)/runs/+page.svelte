<script lang="ts">
	import { onMount } from 'svelte';
	import { invalidateAll } from '$app/navigation';
	import Pill from '$lib/components/Pill.svelte';
	import ScheduleRow from '$lib/components/ScheduleRow.svelte';
	import { once } from '$lib/once';
	import { when, duration } from '$lib/charts/format';
	import { RUN_STATE_LABELS, STAGES, byRecency, isLive, isScheduled } from '$lib/flows';

	let { data, form } = $props();

	const runs = $derived(byRecency(data.runs ?? []));
	const live = $derived(runs.filter((run: any) => isLive(run.state)).length);
	const queued = $derived(runs.filter((run: any) => isScheduled(run.state)).length);
	const mirrored = $derived(runs.some((run: any) => run.source === 'mirror'));
	const canRun = $derived(Boolean(data.health?.available));

	const schedules = $derived((data.schedules ?? []) as any[]);
	const byKey = $derived(new Map(schedules.map((row: any) => [row.key, row])));
	const pendingSync = $derived(schedules.filter((row: any) => row.sync_pending).length);

	/** Расписания, не попавшие ни в один этап, — чтобы ничего не потерялось. */
	const covered = new Set(STAGES.flatMap((stage) => stage.kinds));
	const orphans = $derived(schedules.filter((row: any) => !covered.has(row.kind)));

	/** Пока что-то выполняется, страница обновляется сама.
	 *
	 *  Прогон длится минуты, и человек, запустивший сбор, смотрит именно сюда.
	 *  Заставлять его жать F5 — верный способ получить вопрос «оно вообще
	 *  работает?». Когда всё завершилось, опрос прекращается: дальше меняться
	 *  нечему, а лишние запросы к Prefect ничего не стоят только на словах. */
	onMount(() => {
		const timer = setInterval(() => {
			if (live > 0) invalidateAll();
		}, 5000);
		return () => clearInterval(timer);
	});
</script>

<svelte:head><title>Запуски · Проксима</title></svelte:head>

<div class="spread">
	<h1>Запуски</h1>
	<div class="row">
		{#if !data.health?.available}
			<span class="pill pill-bad">Prefect недоступен — запуск невозможен, правки копятся</span>
		{:else}
			{#if live}
				<span class="pill pill-warn">выполняется: {live} — страница обновляется сама</span>
			{:else}
				<span class="pill pill-good">Prefect на связи</span>
			{/if}
			{#if queued}
				<span class="pill pill-mute">в очереди: {queued}</span>
			{/if}
		{/if}
		{#if pendingSync}
			<form method="POST" action="?/resync" use:once>
				<button type="submit" title="Дослать в Prefect расписания, которые не синхронизировались">
					Дослать правки ({pendingSync})
				</button>
			</form>
		{/if}
	</div>
</div>

{#if form?.error && !form?.id}
	<p class="flash err" role="alert">{form.error}</p>
{/if}
{#if form?.resynced}
	<p class="flash ok" role="status">Досылка выполнена: {JSON.stringify(form.resynced)}</p>
{/if}
{#if data.schedules === null}
	<p class="flash err" role="alert">
		Расписания не загрузились: API не ответил. Это не «расписаний нет» — данных просто не пришло.
	</p>
{/if}

<!-- Этапы конвейера: собрали → отобрали → отправили → прибрали. В каждом —
     таблица расписаний и кнопки-пресеты частых запусков. -->
{#each STAGES as stage}
	{@const rows = schedules.filter((row: any) => stage.kinds.includes(row.kind))}
	<section class="panel stage">
		<header>
			<h2 title={stage.summary}>{stage.title}</h2>
			<div class="presets">
				{#each stage.actions as action}
					{@const target = byKey.get(action.key)}
					<form method="POST" action="?/run" use:once title={action.hint}>
						<input type="hidden" name="id" value={target?.id ?? ''} />
						<input type="hidden" name="label" value={action.label} />
						{#if action.parameters}
							<input type="hidden" name="preset" value={JSON.stringify(action.parameters)} />
						{/if}
						<button type="submit" class:btn-primary={action.primary} disabled={!canRun || !target}>
							{action.label}
						</button>
					</form>
				{/each}
			</div>
		</header>
		{#if rows.length}
			<div class="table-scroll">
				<table class="schedules">
					<thead>
						<tr>
							<th>Флоу</th>
							<th>Период</th>
							<th>Ближайший</th>
							<th>Параметры</th>
							<th>Последний</th>
							<th></th>
						</tr>
					</thead>
					<tbody>
						{#each rows as row (row.id)}
							<ScheduleRow schedule={row} {form} {canRun} />
						{/each}
					</tbody>
				</table>
			</div>
		{:else}
			<p class="empty">Расписаний этого этапа в базе нет — сидирование не отработало.</p>
		{/if}
	</section>
{/each}

{#if orphans.length}
	<section class="panel stage">
		<header><h2>Прочее</h2></header>
		<div class="table-scroll">
			<table class="schedules">
				<tbody>
					{#each orphans as row (row.id)}
						<ScheduleRow schedule={row} {form} {canRun} />
					{/each}
				</tbody>
			</table>
		</div>
	</section>
{/if}

{#if mirrored}
	<p class="muted note">
		Prefect не отвечает, поэтому показана локальная копия из таблицы <code>flow_runs</code>:
		состояния и время в ней есть, логов нет — они живут только в оркестраторе.
	</p>
{/if}

<section class="panel">
	<header>
		<div>
			<h2>История запусков</h2>
			<p class="muted small">
				Что запускалось, чем закончилось и сколько заняло. Логи открываются по строке.
			</p>
		</div>
		<form method="GET" class="filters">
			<select name="kind" aria-label="Флоу">
				<option value="">все флоу</option>
				{#each data.flows as flow}
					<option value={flow.key} selected={data.filters.kind === flow.key}>{flow.name}</option>
				{/each}
			</select>
			<select name="state" aria-label="Состояние">
				<option value="">любое состояние</option>
				{#each Object.entries(RUN_STATE_LABELS) as [value, text]}
					<option {value} selected={data.filters.state === value}>{text}</option>
				{/each}
			</select>
			<button type="submit">Показать</button>
		</form>
	</header>

	{#if runs.length}
		<div class="table-scroll">
			<table>
				<thead>
					<tr>
						<th>Флоу</th>
						<th>Прогон</th>
						<th>Состояние</th>
						<th>Параметры</th>
						<th>Начат</th>
						<th>Завершён</th>
						<th class="num">Длительность</th>
						<th></th>
					</tr>
				</thead>
				<tbody>
					{#each runs as run}
						{@const params = Object.entries(run.parameters ?? {}).filter(
							([key]) => key !== 'bootstrap_target'
						)}
						<tr>
							<td>{run.flow ?? run.name ?? '—'}</td>
							<td class="muted small mono">{run.name ?? '—'}</td>
							<td><Pill status={String(run.state ?? '').toLowerCase()} map={RUN_STATE_LABELS} /></td>
							<td class="muted small mono params">
								{params.length
									? params
											.map(([k, v]) => `${k}=${Array.isArray(v) ? v.join(',') : String(v)}`)
											.join(' ')
									: '—'}
							</td>
							<td class="muted small">{when(run.started_at)}</td>
							<td class="muted small">{when(run.finished_at)}</td>
							<td class="num muted small">{duration(run.duration_seconds)}</td>
							<td class="actions">
								{#if run.source === 'mirror'}
									<span class="muted small">логов нет</span>
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
		<p class="empty">
			{data.filters.kind || data.filters.state
				? 'Под этот фильтр прогонов нет.'
				: 'Прогонов ещё не было. Запустите любой флоу кнопками выше.'}
		</p>
	{/if}
</section>

<style>
	.note {
		max-width: 78ch;
		font-size: 12.5px;
	}

	.small {
		font-size: 12px;
	}

	.stage > header {
		align-items: center;
		padding-top: 6px;
		padding-bottom: 6px;
	}

	.presets {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
		justify-content: flex-end;
	}

	.presets form {
		margin: 0;
	}

	.presets button {
		padding: 3px 10px;
		font-size: 12px;
	}

	.filters {
		display: flex;
		gap: 6px;
		align-items: center;
	}

	.filters select {
		width: auto;
		min-width: 140px;
	}

	.params {
		max-width: 260px;
		white-space: normal;
		word-break: break-word;
	}

	.actions {
		text-align: right;
	}

	.actions .btn {
		padding: 2px 10px;
		font-size: 12px;
		text-decoration: none;
		display: inline-block;
	}

	.flash {
		margin: 0;
		font-size: 12.5px;
	}

	.ok {
		color: var(--good);
	}

	.err {
		color: var(--critical);
	}
</style>
