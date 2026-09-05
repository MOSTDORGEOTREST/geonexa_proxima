<script lang="ts">
	import { onMount } from 'svelte';
	import { enhance } from '$app/forms';
	import { invalidateAll } from '$app/navigation';
	import Pill from '$lib/components/Pill.svelte';
	import { when, duration } from '$lib/charts/format';
	import { RUN_STATE_LABELS, isLive } from '$lib/flows';

	let { data, form } = $props();

	const run = $derived(data.run ?? {});
	// Имя не `state`: `$state` — руна, и Svelte прочитал бы его как обращение к стору.
	const runState = $derived(String(run.state ?? '').toLowerCase());
	const running = $derived(isLive(runState));
	const logs = $derived(data.logs ?? []);

	/** Хвост лога — то, ради чего сюда и заходят. */
	let follow = $state(true);
	let tail: HTMLDivElement | null = $state(null);

	/** Пока прогон не завершился — обновляемся сами, включая ожидание в очереди. */
	const watching = $derived(running || runState === 'scheduled');

	onMount(() => {
		const timer = setInterval(() => {
			if (watching) invalidateAll();
		}, 4000);
		return () => clearInterval(timer);
	});

	$effect(() => {
		// Зависимость от logs.length: прокручиваем, когда пришли новые строки.
		if (follow && logs.length && tail) tail.scrollIntoView({ block: 'end' });
	});

	const LEVELS: Record<number, string> = {
		10: 'DEBUG',
		20: 'INFO',
		30: 'WARNING',
		40: 'ERROR',
		50: 'CRITICAL'
	};

	const levelName = (value: unknown): string => LEVELS[Number(value)] ?? String(value ?? '');

	const time = (value: unknown): string =>
		value
			? new Date(String(value)).toLocaleTimeString('ru-RU', {
					hour: '2-digit',
					minute: '2-digit',
					second: '2-digit'
				})
			: '';
</script>

<svelte:head><title>Прогон {run.flow ?? ''} · Проксима</title></svelte:head>

<div class="spread">
	<div>
		<h1>{run.flow ?? run.name ?? 'Прогон'}</h1>
		<p class="muted mono small">{run.name ?? run.id}</p>
	</div>
	<div class="row">
		<Pill status={runState} map={RUN_STATE_LABELS} />
		{#if running}
			<form method="POST" action="?/cancel" use:enhance>
				<button type="submit" class="btn-danger">Отменить</button>
			</form>
		{/if}
		<a class="btn" href="/runs">К списку</a>
	</div>
</div>

{#if form?.error}<p class="err" role="alert">{form.error}</p>{/if}
{#if form?.cancelled}<p class="ok" role="status">Отмена отправлена в Prefect.</p>{/if}

<section class="facts">
	<div class="tile">
		<span class="label">Начат</span>
		<span class="value small">{when(run.started_at)}</span>
	</div>
	<div class="tile">
		<span class="label">Завершён</span>
		<span class="value small">{when(run.finished_at)}</span>
	</div>
	<div class="tile">
		<span class="label">Длительность</span>
		<span class="value small">{duration(run.duration_seconds)}</span>
	</div>
	<div class="tile">
		<span class="label">Строк лога</span>
		<span class="value small">{logs.length}</span>
	</div>
</section>

{#if run.parameters && Object.keys(run.parameters).length}
	<section class="panel">
		<header><h2>Параметры запуска</h2></header>
		<div class="params">
			{#each Object.entries(run.parameters) as [key, value]}
				<div class="param">
					<span class="muted small">{key}</span>
					<code>{JSON.stringify(value)}</code>
				</div>
			{/each}
		</div>
	</section>
{/if}

<section class="panel">
	<header>
		<h2>Лог</h2>
		<label class="follow">
			<input type="checkbox" bind:checked={follow} />
			следить за хвостом
		</label>
	</header>

	{#if logs.length}
		<div class="log">
			{#each logs as line}
				<div class="line level-{levelName(line.level).toLowerCase()}">
					<span class="ts">{time(line.timestamp)}</span>
					<span class="lvl">{levelName(line.level)}</span>
					<span class="msg">{line.message}</span>
				</div>
			{/each}
			<div bind:this={tail}></div>
		</div>
	{:else if runState === 'scheduled'}
		<p class="empty">
			Прогон ждёт свободного воркера — он ещё не стартовал, и логу взяться неоткуда.
		</p>
	{:else if running}
		<p class="empty">Прогон только начался — строк ещё нет. Страница обновится сама.</p>
	{:else}
		<p class="empty">
			Логов нет. Так бывает, когда процесс упал до того, как флоу успел записать первую
			строку, — причина тогда в логе воркера: <code>docker compose logs prefect-worker</code>.
		</p>
	{/if}
</section>

<style>
	h1 {
		margin-bottom: 2px;
	}

	.small {
		font-size: 12px;
	}

	.facts {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
		gap: var(--gap);
		margin-bottom: var(--gap);
	}

	.facts .value {
		font-family: var(--font-s);
		font-size: 15px;
		font-weight: 500;
	}

	.params {
		display: grid;
		gap: 8px;
		padding: 14px 18px;
	}

	.param {
		display: flex;
		gap: 10px;
		align-items: baseline;
	}

	.follow {
		display: flex;
		align-items: center;
		gap: 6px;
		text-transform: none;
		letter-spacing: 0;
		font-size: 12.5px;
	}

	.follow input {
		width: auto;
	}

	/* Лог — единственное место, где моноширинный текст обязателен: колонки
	   времени и уровня должны стоять друг под другом, иначе глаз не находит
	   строку, с которой всё пошло не так. */
	.log {
		max-height: 60vh;
		overflow: auto;
		padding: 12px 18px 16px;
		font-family: var(--font-m);
		font-size: 12px;
		line-height: 1.6;
	}

	.line {
		display: grid;
		grid-template-columns: 68px 72px 1fr;
		gap: 10px;
		border-bottom: 1px solid var(--border-soft);
		padding: 2px 0;
	}

	.ts {
		color: var(--muted);
		font-variant-numeric: tabular-nums;
	}

	.lvl {
		color: var(--muted);
	}

	.msg {
		white-space: pre-wrap;
		overflow-wrap: anywhere;
	}

	.level-warning .lvl,
	.level-warning .msg {
		color: var(--warning);
	}

	.level-error .lvl,
	.level-error .msg,
	.level-critical .lvl,
	.level-critical .msg {
		color: var(--critical);
	}

	.err {
		color: var(--critical);
	}

	.ok {
		color: var(--good);
	}

	.btn {
		text-decoration: none;
	}
</style>
