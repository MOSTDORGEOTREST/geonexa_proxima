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
	<h1>Сбор</h1>
	<div class="row">
		{#if !data.health?.available}
			<span class="pill pill-bad">Prefect недоступен — запуск невозможен</span>
		{/if}
		<form method="POST" action="?/collect" use:once>
			<input type="hidden" name="id" value={data.harvestSchedule?.id ?? ''} />
			<input type="hidden" name="label" value="Сбор материалов" />
			<button type="submit" class="btn-primary" disabled={!canRun} title="Вчерашние сутки плюс догон пропущенных — то же, что делает расписание">
				Собрать
			</button>
		</form>
		<form method="POST" action="?/collect" use:once>
			<input type="hidden" name="id" value={data.harvestSchedule?.id ?? ''} />
			<input type="hidden" name="label" value="Сбор за 30 дней" />
			<input type="hidden" name="parameters" value={JSON.stringify({ days_back: 30, limit_per_source: 1000 })} />
			<button type="submit" disabled={!canRun} title="Тридцать суток по одним, тридцать проходов">За 30 дней</button>
		</form>
		<a class="btn" href="/runs" title="Период, параметры и другие расписания">Расписание</a>
	</div>
</div>

{#if form?.error}<p class="flash err" role="alert">{form.error}</p>{/if}
{#if form?.started}
	<p class="flash ok" role="status">«{form.label}» — запуск поставлен в очередь Prefect.</p>
{/if}
{#if form?.aborted !== undefined}
	<p class="flash ok" role="status">Снято прогонов: {form.aborted}. Теперь сбор можно запускать.</p>
{/if}

{#if data.activeRun}
	<!-- Одновременно идёт только один сбор: пока эта запись висит, кнопки
	     отвечают «сбор уже идёт», и без объяснения это выглядит как поломка. -->
	<div class="active">
		<span>
			<b>Идёт прогон</b>
			<span class="muted small">
				с {when(data.activeRun.started_at)} ({data.activeRun.trigger})
				{#if data.activeRun.stats?.days_planned}
					· пройдено {data.activeRun.stats.days_done ?? 0} из {data.activeRun.stats.days_planned} суток,
					отметка {when(data.activeRun.stats.heartbeat_at)}
				{/if}
			</span>
		</span>
		<form method="POST" action="?/abort" use:once>
			<button type="submit" class="btn-danger" title="Если процесс упал, не закрыв запись: сам он подберётся только по таймауту">
				Снять прогон
			</button>
		</form>
	</div>
{/if}

<div class="two">
	<section class="panel">
		<header>
			<h2>Проба гейта</h2>
			<span class="muted">без записи в базу</span>
		</header>
		<div class="body">
			<form method="POST" action="?/probe" use:enhance class="probe">
				<input name="title" value={form?.title ?? ''} required placeholder="Заголовок статьи — какие термины сработают и какая группа не выполнится" />
				<textarea name="abstract" rows="2" placeholder="Аннотация (необязательно)"></textarea>
				<div class="row">
					<input name="venue" placeholder="Журнал (необязательно)" class="venue" />
					<button type="submit" class="btn-primary">Проверить</button>
				</div>
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
		<header>
			<h2>Профиль</h2>
			<form method="POST" action="?/resync" use:once>
				<button type="submit" title="Перечитать config/harvest.yaml: правило, группы, термины">
					Обновить из файла
				</button>
			</form>
		</header>
		<div class="body">
			{#if form?.resynced}
				<p class="flash ok" role="status">
					Профиль перечитан: групп {form.resynced.groups_upserted}, терминов {form.resynced.terms_upserted},
					удалено терминов {form.resynced.terms_removed}.
				</p>
			{/if}
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

	.small {
		font-size: 12.5px;
	}

	.probe {
		display: grid;
		gap: 6px;
	}

	.probe .venue {
		flex: 1;
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

	.flash {
		margin: 0;
		font-size: 12.5px;
	}

	.active {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--gap);
		flex-wrap: wrap;
		padding: 8px 12px;
		border: 1px solid color-mix(in srgb, var(--warning) 40%, transparent);
		border-radius: var(--r-card);
		font-size: 13px;
	}

	.active b {
		margin-right: 6px;
	}

	.ok {
		color: var(--good);
	}
</style>
