<script lang="ts">
	import { once } from '$lib/once';
	import { enhance } from '$app/forms';
	import Pill from '$lib/components/Pill.svelte';
	import { n, when } from '$lib/charts/format';

	let { data, form } = $props();
</script>

<svelte:head><title>Подписки · Проксима</title></svelte:head>

<h1>Подписки</h1>

{#if form?.error}<p class="err" role="alert">{form.error}</p>{/if}

{#if data.expiring.length}
	<section class="panel warn">
		<header>
			<h2>Истекают в ближайшую неделю</h2>
			<span class="muted">{data.expiring.length}</span>
		</header>
		<div class="table-scroll">
			<table>
				<thead><tr><th>Тариф</th><th>Заканчивается</th><th></th></tr></thead>
				<tbody>
					{#each data.expiring as row}
						<tr>
							<td>{row.plan_name}</td>
							<td class="muted">{when(row.ends_at)}</td>
							<td class="actions">
								<form method="POST" action="?/extend" use:once>
									<input type="hidden" name="id" value={row.id} />
									<input type="hidden" name="days" value="30" />
									<button type="submit">Продлить на 30 дней</button>
								</form>
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	</section>
{/if}

<section class="panel">
	<header><h2>Тарифы</h2></header>
	<div class="table-scroll">
		<table>
			<thead>
				<tr>
					<th>Ключ</th><th>Название</th><th class="num">Профилей</th>
					<th class="num">Материалов</th><th class="num">Интервал, ч</th><th>Чаты</th>
				</tr>
			</thead>
			<tbody>
				{#each data.plans as plan}
					<tr>
						<td class="mono small">{plan.key}{plan.is_default ? ' ·' : ''}</td>
						<td>{plan.name}</td>
						<td class="num">{n(plan.max_profiles)}</td>
						<td class="num">{n(plan.max_items_per_digest)}</td>
						<td class="num">{n(plan.min_interval_hours)}</td>
						<td>
							{#if plan.allow_group_chats}
								<span class="pill pill-good">разрешены</span>
							{:else}
								<span class="pill pill-mute">только личка</span>
							{/if}
						</td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>
</section>

<section class="panel">
	<header><h2>Выданные подписки</h2></header>
	{#if data.list?.items?.length}
		<div class="table-scroll">
			<table>
				<thead>
					<tr>
						<th>Подписчик</th><th>Тариф</th><th>Статус</th>
						<th>Начало</th><th>Окончание</th><th></th>
					</tr>
				</thead>
				<tbody>
					{#each data.list.items as row}
						<tr>
							<td>{row.subscriber_title ?? row.telegram_chat_id}</td>
							<td>{row.plan_name}</td>
							<td><Pill status={row.status} /></td>
							<td class="muted">{when(row.starts_at)}</td>
							<td class="muted">{row.ends_at ? when(row.ends_at) : 'бессрочно'}</td>
							<td class="actions">
								{#if ['active', 'trial'].includes(row.status)}
									<form method="POST" action="?/cancel" use:once>
										<input type="hidden" name="id" value={row.id} />
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
		<p class="empty">Подписок пока нет.</p>
	{/if}
</section>

<style>
	.warn {
		border-color: color-mix(in srgb, var(--warning) 45%, var(--border));
	}

	.small {
		font-size: 12px;
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
