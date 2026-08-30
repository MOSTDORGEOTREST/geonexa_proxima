<script lang="ts">
	import { n, when } from '$lib/charts/format';
	let { data } = $props();
</script>

<svelte:head><title>Аудит · Проксима</title></svelte:head>

<div class="spread">
	<h1>Журнал действий</h1>
	{#if data.log}<span class="muted">{n(data.log.total)} записей</span>{/if}
</div>

<section class="panel">
	{#if data.log?.items?.length}
		<div class="table-scroll">
			<table>
				<thead>
					<tr><th>Когда</th><th>Кто</th><th>Действие</th><th>Объект</th><th>Детали</th><th>IP</th></tr>
				</thead>
				<tbody>
					{#each data.log.items as row}
						<tr>
							<td class="muted">{when(row.created_at)}</td>
							<td>{row.actor}</td>
							<td class="mono small">{row.action}</td>
							<td class="muted small">{row.entity_type ?? '—'}</td>
							<td class="dim small">{row.after ? JSON.stringify(row.after) : '—'}</td>
							<td class="muted mono small">{row.ip ?? '—'}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	{:else}
		<p class="empty">Действий пока не было.</p>
	{/if}
</section>

<style>
	.small {
		font-size: 12px;
		max-width: 44ch;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
</style>
