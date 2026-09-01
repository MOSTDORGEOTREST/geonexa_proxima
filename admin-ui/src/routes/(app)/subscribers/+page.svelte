<script lang="ts">
	import { enhance } from '$app/forms';
	import Pill from '$lib/components/Pill.svelte';
	import { n, when } from '$lib/charts/format';

	let { data, form } = $props();

	const KIND = { user: 'личный', group: 'группа', channel: 'канал' } as const;
	const STATUS = {
		active: 'активен',
		pending: 'ждёт',
		paused: 'пауза',
		blocked: 'заблокирован',
		left: 'ушёл'
	};
</script>

<svelte:head><title>Подписчики · Проксима</title></svelte:head>

<div class="spread">
	<h1>Подписчики</h1>
	{#if data.list}
		<span class="muted">{n(data.list.total)} всего</span>
	{/if}
</div>

{#if data.breakdown?.rows?.length}
	<div class="row">
		{#each data.breakdown.rows as row}
			<span class="pill pill-mute">
				{KIND[row.kind as keyof typeof KIND] ?? row.kind} · {STATUS[row.status as keyof typeof STATUS] ??
					row.status}: {row.count}
			</span>
		{/each}
	</div>
{/if}

<form method="GET" class="filters">
	<input name="q" placeholder="Имя, @username или chat_id" value={data.filters.q ?? ''} />
	<select name="kind">
		<option value="">Все виды</option>
		<option value="user" selected={data.filters.kind === 'user'}>Личные чаты</option>
		<option value="group" selected={data.filters.kind === 'group'}>Группы</option>
		<option value="channel" selected={data.filters.kind === 'channel'}>Каналы</option>
	</select>
	<select name="status">
		<option value="">Любой статус</option>
		{#each Object.entries(STATUS) as [value, label]}
			<option {value} selected={data.filters.status === value}>{label}</option>
		{/each}
	</select>
	<button type="submit">Фильтр</button>
</form>

{#if form?.error}<p class="err" role="alert">{form.error}</p>{/if}

<section class="panel">
	{#if data.list?.items?.length}
		<div class="table-scroll">
			<table>
				<thead>
					<tr>
						<th>Кто</th><th>Вид</th><th>chat_id</th><th>Статус</th>
						<th>Последняя активность</th><th></th>
					</tr>
				</thead>
				<tbody>
					{#each data.list.items as row}
						<tr>
							<td>
								<a href="/subscribers/{row.id}">{row.title || row.telegram_username || '—'}</a>
							</td>
							<td class="muted">{KIND[row.kind as keyof typeof KIND] ?? row.kind}</td>
							<td class="mono muted">{row.telegram_chat_id}</td>
							<td><Pill status={row.status} map={STATUS} /></td>
							<td class="muted">{when(row.last_seen_at)}</td>
							<td class="actions">
								{#if row.status !== 'active'}
									<form method="POST" action="?/approve" use:enhance>
										<input type="hidden" name="id" value={row.id} />
										<button type="submit">Активировать</button>
									</form>
								{:else}
									<form method="POST" action="?/block" use:enhance>
										<input type="hidden" name="id" value={row.id} />
										<button type="submit" class="btn-danger">Заблокировать</button>
									</form>
								{/if}
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	{:else}
		<p class="empty" class:err={!data.list}>
			{data.list
				? 'Подписчиков по этому фильтру нет.'
				: 'Список не загрузился: API не ответил. Это не «подписчиков нет» — данных просто не пришло.'}
		</p>
	{/if}
</section>

<style>
	.filters {
		display: grid;
		grid-template-columns: 2fr 1fr 1fr auto;
		gap: var(--gap);
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

	@media (max-width: 720px) {
		.filters {
			grid-template-columns: 1fr;
		}
	}
</style>
