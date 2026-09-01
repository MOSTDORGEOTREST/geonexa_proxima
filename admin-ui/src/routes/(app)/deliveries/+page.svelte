<script lang="ts">
	import { enhance } from '$app/forms';
	import Pill from '$lib/components/Pill.svelte';
	import { duration, n, when } from '$lib/charts/format';

	let { data, form } = $props();

	const CHANNEL = { personal: 'личные', group: 'чаты' };
</script>

<svelte:head><title>Доставки · Проксима</title></svelte:head>

<div class="spread">
	<h1>Очередь доставки</h1>
	{#if data.queue?.stuck}
		<span class="pill pill-warn">зависших заданий: {data.queue.stuck}</span>
	{/if}
</div>

{#if form?.error}<p class="err" role="alert">{form.error}</p>{/if}

<section class="panel">
	<header><h2>Состояние очереди</h2><span class="muted">по каналам</span></header>
	{#if data.queue?.rows?.length}
		<div class="table-scroll">
			<table>
				<thead>
					<tr><th>Канал</th><th>Статус</th><th class="num">Заданий</th><th>Ждёт дольше всех</th></tr>
				</thead>
				<tbody>
					{#each data.queue.rows as row}
						<tr>
							<td>{CHANNEL[row.channel as keyof typeof CHANNEL] ?? row.channel}</td>
							<td><Pill status={row.status} /></td>
							<td class="num">{n(row.n)}</td>
							<td class="muted">{duration(row.oldest_age_seconds)}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	{:else}
		<p class="empty" class:err={!data.jobs}>
			{data.jobs
				? 'Очередь пуста.'
				: 'Очередь не загрузилась: API не ответил. Задания могут быть на месте.'}
		</p>
	{/if}
</section>

<form method="GET" class="filters">
	<select name="channel">
		<option value="">Все каналы</option>
		<option value="personal" selected={data.filters.channel === 'personal'}>Личные</option>
		<option value="group" selected={data.filters.channel === 'group'}>Чаты</option>
	</select>
	<select name="status">
		<option value="">Любой статус</option>
		{#each ['queued', 'claimed', 'sending', 'sent', 'failed', 'cancelled', 'skipped'] as s}
			<option value={s} selected={data.filters.status === s}>{s}</option>
		{/each}
	</select>
	<button type="submit">Фильтр</button>
</form>

<section class="panel">
	{#if data.jobs?.items?.length}
		<div class="table-scroll">
			<table>
				<thead>
					<tr>
						<th>Кому</th><th>Канал</th><th>Статус</th><th class="num">Попыток</th>
						<th>Следующая</th><th>Ошибка</th><th></th>
					</tr>
				</thead>
				<tbody>
					{#each data.jobs.items as job}
						<tr>
							<td>{job.subscriber_title ?? job.target_chat_id}</td>
							<td class="muted">{CHANNEL[job.channel as keyof typeof CHANNEL] ?? job.channel}</td>
							<td><Pill status={job.status} /></td>
							<td class="num">{job.attempts}/{job.max_attempts}</td>
							<td class="muted">{when(job.next_retry_at)}</td>
							<td class="dim small">{job.last_error ?? '—'}</td>
							<td class="actions">
								{#if ['failed', 'cancelled', 'skipped'].includes(job.status)}
									<form method="POST" action="?/retry" use:enhance>
										<input type="hidden" name="id" value={job.id} />
										<button type="submit">Повторить</button>
									</form>
								{:else if job.status === 'queued'}
									<form method="POST" action="?/cancel" use:enhance>
										<input type="hidden" name="id" value={job.id} />
										<button type="submit" class="btn-danger">Отменить</button>
									</form>
								{/if}
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	{:else}
		<p class="empty">Заданий по фильтру нет.</p>
	{/if}
</section>

<style>
	.filters {
		display: grid;
		grid-template-columns: 1fr 1fr auto;
		gap: var(--gap);
		max-width: 520px;
	}

	.small {
		font-size: 12px;
		max-width: 40ch;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.actions {
		text-align: right;
	}

	.actions button {
		padding: 4px 12px;
		font-size: 12.5px;
	}

	.err {
		color: var(--critical);
	}
</style>
