<script lang="ts">
	import { enhance } from '$app/forms';

	let { data, form } = $props();
	let filter = $state('');

	const visible = $derived(
		data.settings.filter((row: any) =>
			filter ? row.key.toLowerCase().includes(filter.toLowerCase()) : true
		)
	);
	const show = (value: unknown): string =>
		value === null || value === undefined ? '—' : typeof value === 'string' ? value : JSON.stringify(value);
</script>

<svelte:head><title>Настройки · Проксима</title></svelte:head>

<h1>Настройки</h1>

<p class="muted note">
	Колонка «действует» — то, что реально применяется: значение из базы, если оно задано, иначе из
	<code>.env</code>. Часть настроек читается до подключения к базе или даёт доступ к ней самой —
	такие меняются только через <code>.env</code> и здесь заблокированы. Изменения вступают в силу
	после перезапуска сервиса.
</p>

{#if form?.error}<p class="err" role="alert">{form.error}</p>{/if}

{#if data.diff.length}
	<section class="panel">
		<header><h2>Переопределено относительно .env</h2><span class="muted">{data.diff.length}</span></header>
		<div class="table-scroll">
			<table>
				<thead><tr><th>Ключ</th><th>В .env</th><th>Сейчас</th><th>Кем</th></tr></thead>
				<tbody>
					{#each data.diff as row}
						<tr>
							<td class="mono small">{row.key}</td>
							<td class="muted small">{show(row.env_default)}</td>
							<td class="small">{show(row.value)}</td>
							<td class="muted small">{row.updated_by}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	</section>
{/if}

<input placeholder="Поиск по ключу" bind:value={filter} class="search" />

<section class="panel">
	<div class="table-scroll">
		<table>
			<thead>
				<tr><th>Ключ</th><th>Область</th><th>Действует</th><th>Новое значение</th><th></th></tr>
			</thead>
			<tbody>
				{#each visible as row}
					<tr>
						<td class="mono small">{row.key}</td>
						<td class="muted small">{row.scope}</td>
						<td class="small">{show(row.effective)}</td>
						<td>
							{#if row.is_env_only}
								<span class="pill pill-mute">только .env</span>
							{:else}
								<form method="POST" action="?/set" use:enhance class="setter">
									<input type="hidden" name="key" value={row.key} />
									<input name="value" placeholder={show(row.effective)} />
									<button type="submit">↵</button>
								</form>
							{/if}
						</td>
						<td class="actions">
							{#if row.overridden && !row.is_env_only}
								<form method="POST" action="?/reset" use:enhance>
									<input type="hidden" name="key" value={row.key} />
									<button type="submit">К .env</button>
								</form>
							{/if}
						</td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>
</section>

<style>
	.note {
		max-width: 80ch;
		font-size: 13px;
		margin: 0;
	}

	.search {
		max-width: 320px;
	}

	.small {
		font-size: 12.5px;
		max-width: 46ch;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.setter {
		display: flex;
		gap: 4px;
	}

	.setter input {
		min-width: 160px;
	}

	.setter button,
	.actions button {
		padding: 4px 10px;
		font-size: 12.5px;
	}

	.actions {
		text-align: right;
	}

	.err {
		color: var(--critical);
	}
</style>
