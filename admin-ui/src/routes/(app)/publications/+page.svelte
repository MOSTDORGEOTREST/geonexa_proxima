<script lang="ts">
	import Pager from '$lib/components/Pager.svelte';
	import { n, day, when } from '$lib/charts/format';

	let { data } = $props();

	const KIND: Record<string, string> = {
		paper: 'статья',
		method: 'метод',
		software: 'код',
		dataset: 'датасет'
	};
	const SOURCE: Record<string, string> = {
		arxiv: 'arXiv',
		openalex: 'OpenAlex',
		crossref: 'Crossref',
		semantic_scholar: 'Semantic Scholar',
		github: 'GitHub',
		huggingface: 'Hugging Face',
		cyberleninka: 'КиберЛенинка',
		oai: 'журнал (OAI)'
	};
	const LANGUAGE: Record<string, string> = {
		ru: 'русский',
		en: 'английский',
		unknown: 'не указан',
		other: 'другой'
	};
	const SORT: Record<string, string> = {
		created: 'сначала новые в корпусе',
		published: 'по дате публикации',
		score: 'по оценке',
		semantic: 'по близости к профилю',
		citations: 'по цитированиям',
		title: 'по заголовку'
	};

	const list = $derived(data.list);
	const f = $derived(data.filters as Record<string, string>);
	const active = $derived(Object.keys(f).filter((key) => key !== 'sort').length);

	const score = (value: unknown): string =>
		value === null || value === undefined ? '—' : Number(value).toFixed(1);
	const cosine = (value: unknown): string =>
		value === null || value === undefined ? '—' : Number(value).toFixed(2);
	const link = (row: any): string | null =>
		row.canonical_url ||
		(row.doi ? `https://doi.org/${row.doi}` : null) ||
		(row.arxiv_id ? `https://arxiv.org/abs/${row.arxiv_id}` : null);
</script>

<svelte:head><title>Публикации · Проксима</title></svelte:head>

<div class="spread">
	<h1>Публикации</h1>
	{#if data.facets?.totals}
		<div class="row">
			<span class="pill pill-mute">в корпусе: {n(data.facets.totals.total)}</span>
			<span class="pill pill-mute">оценено: {n(data.facets.totals.scored)}</span>
			<span class="pill pill-mute">разобрано глубоко: {n(data.facets.totals.analyzed)}</span>
			<span class="pill pill-mute">за неделю: +{n(data.facets.totals.last_week)}</span>
		</div>
	{/if}
</div>

<!-- Фильтры — обычная GET-форма: состояние живёт в адресе, страницу можно
     переслать коллеге, а «назад» возвращает к прежнему срезу. -->
<form method="GET" class="filters panel">
	<div class="line">
		<input
			name="q"
			placeholder="Заголовок, аннотация, автор, DOI или журнал"
			value={f.q ?? ''}
			aria-label="Поиск"
		/>
		<select name="source" aria-label="Источник">
			<option value="">все источники</option>
			{#each data.facets?.sources ?? [] as row}
				<option value={row.source} selected={f.source === row.source}>
					{SOURCE[row.source] ?? row.source} ({n(row.n)})
				</option>
			{/each}
		</select>
		<select name="kind" aria-label="Вид">
			<option value="">все виды</option>
			{#each data.facets?.kinds ?? [] as row}
				<option value={row.kind} selected={f.kind === row.kind}>
					{KIND[row.kind] ?? row.kind} ({n(row.n)})
				</option>
			{/each}
		</select>
		<select name="language" aria-label="Язык">
			<option value="">любой язык</option>
			{#each data.facets?.languages ?? [] as row}
				<option value={row.language} selected={f.language === row.language}>
					{LANGUAGE[row.language] ?? row.language} ({n(row.n)})
				</option>
			{/each}
		</select>
		<select name="sort" aria-label="Сортировка">
			{#each Object.entries(SORT) as [value, label]}
				<option {value} selected={(f.sort ?? 'created') === value}>{label}</option>
			{/each}
		</select>
		<button type="submit" class="btn-primary">Показать</button>
	</div>
	<details class="more" open={active > 0}>
		<summary class="muted">Ещё фильтры{active ? ` · активно: ${active}` : ''}</summary>
		<div class="line">
			<select name="scored" aria-label="Оценка LLM">
				<option value="">оценка: любая</option>
				<option value="true" selected={f.scored === 'true'}>только оценённые</option>
				<option value="false" selected={f.scored === 'false'}>только без оценки</option>
			</select>
			<select name="analyzed" aria-label="Глубокий разбор">
				<option value="">разбор: любой</option>
				<option value="true" selected={f.analyzed === 'true'}>разобраны глубоко</option>
				<option value="false" selected={f.analyzed === 'false'}>без разбора</option>
			</select>
			<label class="inline">
				<span>оценка от</span>
				<input name="min_score" type="number" min="0" max="10" step="0.5" value={f.min_score ?? ''} />
			</label>
			<label class="inline">
				<span>опубликовано с</span>
				<input name="date_from" type="date" value={f.date_from ?? ''} />
			</label>
			<label class="inline">
				<span>по</span>
				<input name="date_to" type="date" value={f.date_to ?? ''} />
			</label>
			<label class="inline">
				<span>в корпусе с</span>
				<input name="created_from" type="date" value={f.created_from ?? ''} />
			</label>
			<label class="inline">
				<span>по</span>
				<input name="created_to" type="date" value={f.created_to ?? ''} />
			</label>
			{#if active}
				<a class="btn" href="/publications">Сбросить</a>
			{/if}
		</div>
	</details>
</form>

<section class="panel">
	{#if list?.items?.length}
		<div class="table-scroll">
			<table>
				<thead>
					<tr>
						<th>Публикация</th>
						<th>Вид</th>
						<th>Источники</th>
						<th>Дата</th>
						<th class="num" title="Глобальная оценка LLM, 0–10">Оценка</th>
						<th class="num" title="Косинус к профилю сбора">Близость</th>
						<th class="num">Цит.</th>
						<th>В корпусе</th>
					</tr>
				</thead>
				<tbody>
					{#each list.items as row}
						<tr>
							<td class="title-cell">
								<a href="/publications/{row.id}" class="title">{row.title}</a>
								{#if row.authors}
									<span class="muted small authors">{row.authors}</span>
								{/if}
								{#if row.venue}
									<span class="muted small">· {row.venue}</span>
								{/if}
								{#if row.rank_reason}
									<p class="dim small reason">{row.rank_reason}</p>
								{/if}
							</td>
							<td class="muted">{KIND[row.kind] ?? row.kind}</td>
							<td class="muted small">
								{(row.sources ?? []).map((s: string) => SOURCE[s] ?? s).join(', ') || '—'}
								{#if link(row)}
									<a href={link(row)} target="_blank" rel="noopener" class="ext">↗</a>
								{/if}
							</td>
							<td class="muted small">{row.publication_date ?? '—'}</td>
							<td class="num">
								{#if row.rank_total_score !== null && row.rank_total_score !== undefined}
									<span class="score" class:hot={row.rank_total_score >= 8}>
										{score(row.rank_total_score)}
									</span>
									{#if row.analyzed}<span class="muted small" title="есть глубокий разбор"> ★</span>{/if}
								{:else}
									<span class="muted">—</span>
								{/if}
							</td>
							<td class="num muted mono small">{cosine(row.semantic_score)}</td>
							<td class="num muted small">{n(row.citation_count)}</td>
							<td class="muted small" title={when(row.created_at)}>{day(row.created_at)}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
		<Pager current={list.page} pages={list.pages} total={list.total} perPage={list.per_page} />
	{:else}
		<p class="empty" class:err={!list}>
			{list
				? 'Под этот фильтр публикаций нет.'
				: 'Список не загрузился: API не ответил. Это не «корпус пуст» — данных просто не пришло.'}
		</p>
	{/if}
</section>

<style>
	.filters {
		display: grid;
		gap: 10px;
		padding: 14px 18px;
		margin-bottom: var(--gap);
	}

	.line {
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
		align-items: end;
	}

	.line input[name='q'] {
		flex: 2 1 320px;
	}

	.line select {
		width: auto;
		min-width: 170px;
		flex: 0 1 auto;
	}

	.line input[type='date'],
	.line input[type='number'] {
		width: auto;
		min-width: 120px;
	}

	.inline {
		gap: 4px;
	}

	.more summary {
		cursor: pointer;
		font-size: 12.5px;
	}

	.more[open] summary {
		margin-bottom: 8px;
	}

	.title-cell {
		height: auto;
		padding-top: 8px;
		padding-bottom: 8px;
		max-width: 640px;
	}

	.title {
		display: block;
		font-weight: 500;
		line-height: 1.35;
	}

	.authors {
		display: inline-block;
		max-width: 420px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		vertical-align: bottom;
	}

	.reason {
		margin: 4px 0 0;
		line-height: 1.4;
		max-width: 70ch;
	}

	.small {
		font-size: 12px;
	}

	.score {
		font-family: var(--font-m);
		font-weight: 500;
	}

	.hot {
		color: var(--accent);
	}

	.ext {
		text-decoration: none;
		margin-left: 4px;
	}

	.err {
		color: var(--critical);
	}
</style>
