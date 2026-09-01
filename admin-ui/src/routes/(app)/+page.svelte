<script lang="ts">
	import StatTile from '$lib/components/StatTile.svelte';
	import LineChart from '$lib/charts/LineChart.svelte';
	import FunnelBar from '$lib/charts/FunnelBar.svelte';
	import { ago, n, when } from '$lib/charts/format';

	let { data } = $props();
	const s = $derived(data.summary);

	const funnelSteps = $derived(
		s
			? [
					{ label: 'Собрано', value: s.harvest.funnel.fetched ?? 0 },
					{ label: 'Принято', value: s.harvest.funnel.accepted ?? 0 },
					{ label: 'Пограничные', value: s.harvest.funnel.borderline ?? 0 },
					{ label: 'Отклонено', value: s.harvest.funnel.rejected ?? 0 }
				]
			: []
	);
</script>

<svelte:head><title>Дашборд · Проксима</title></svelte:head>

<div class="spread">
	<h1>Что происходит</h1>
	<nav class="row">
		{#each [7, 30, 90] as d}
			<a href="?days={d}" class="pill" class:pill-good={data.days === d}>{d} дней</a>
		{/each}
	</nav>
</div>

{#if !s}
	<p class="empty">API не отвечает. Проверьте, что сервис запущен и база доступна.</p>
{:else}
	<section class="tiles">
		<StatTile label="Материалов" value={s.corpus.items} hint="+{n(s.corpus.new_24h)} за сутки" />
		<StatTile label="Оценено" value={s.corpus.ranked} hint="разобрано {n(s.corpus.analyzed)}" />
		<StatTile
			label="Подписчиков"
			value={s.subscribers.active}
			hint="{n(s.subscribers.users)} личных · {n(s.subscribers.groups)} групп · {n(
				s.subscribers.channels
			)} каналов"
		/>
		<StatTile
			label="Подписок"
			value={s.subscriptions.active}
			hint="истекают за 7 дней: {n(s.subscriptions.expiring_7d)}"
			tone={s.subscriptions.expiring_7d > 0 ? 'warn' : 'neutral'}
		/>
		<StatTile
			label="В очереди"
			value={s.delivery.queued}
			hint="отправлено {n(s.delivery.sent_24h)} за сутки"
		/>
		<StatTile
			label="Провалов"
			value={s.delivery.failed_24h}
			hint="за последние сутки"
			tone={s.delivery.failed_24h > 0 ? 'bad' : 'good'}
		/>
		<StatTile
			label="Вызовов LLM"
			value={s.llm.calls_24h}
			hint="{n(s.llm.tokens_24h)} токенов · ${s.llm.cost_24h.toFixed(2)}"
		/>
		<StatTile
			label="Ждут одобрения"
			value={s.subscribers.pending}
			tone={s.subscribers.pending > 0 ? 'warn' : 'neutral'}
		/>
	</section>

	<div class="two">
		<section class="panel">
			<header>
				<h2>Воронка сбора</h2>
				<span class="muted">
					{#if s.harvest.last_run}
						последний прогон {ago(s.harvest.last_run.started_at)}
					{:else}
						прогонов ещё не было
					{/if}
				</span>
			</header>
			<div class="body">
				{#if funnelSteps.some((step) => step.value)}
					<FunnelBar steps={funnelSteps} />
				{:else}
					<p class="empty">Сбор ещё не запускался.</p>
				{/if}
			</div>
		</section>

		<section class="panel">
			<header><h2>Материалы и доставки</h2></header>
			<div class="body">
				<LineChart
					points={data.timeline?.points ?? []}
					series={data.timeline?.series ?? []}
					area
				/>
			</div>
		</section>
	</div>

	<section class="panel">
		<header>
			<h2>Последние ошибки</h2>
			<span class="muted">сбор, доставка и флоу одной лентой</span>
		</header>
		{#if s.errors?.length}
			<div class="table-scroll">
				<table>
					<thead>
						<tr><th>Где</th><th>Когда</th><th>Что</th></tr>
					</thead>
					<tbody>
						{#each s.errors as error}
							<tr>
								<td><span class="pill pill-bad">{error.source}</span></td>
								<td class="muted">{when(error.at)}</td>
								<td class="dim">{error.message}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{:else}
			<p class="empty">Ошибок нет.</p>
		{/if}
	</section>
{/if}

<style>
	.tiles {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
		gap: var(--gap);
	}

	.two {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(380px, 1fr));
		gap: var(--gap);
	}

	a.pill {
		text-decoration: none;
	}
</style>
