<script lang="ts">
	import Pill from '$lib/components/Pill.svelte';
	import { n, when } from '$lib/charts/format';

	let { data } = $props();

	const item = $derived(data.item);
	const ranking = $derived(item?.ranking ?? null);
	const analysis = $derived(item?.deep_analysis ?? null);

	const KIND: Record<string, string> = {
		paper: 'статья',
		method: 'метод',
		software: 'код',
		dataset: 'датасет'
	};
	const DECISION = { accepted: 'принято', borderline: 'пограничное', rejected: 'отклонено' };
	const DIMENSIONS: Array<[string, string]> = [
		['relevance', 'релевантность'],
		['novelty', 'новизна'],
		['scientific_quality', 'научное качество'],
		['practical_value', 'практическая ценность'],
		['importance_for_geotechnics', 'важность для геотехники'],
		['importance_for_ai', 'важность для AI']
	];
	const SECTIONS: Array<[string, string]> = [
		['summary', 'Суть'],
		['novelty', 'Новизна'],
		['method', 'Метод'],
		['data', 'Данные'],
		['architecture', 'Архитектура'],
		['results', 'Результаты'],
		['prior_art', 'Предшественники'],
		['physics_assessment', 'Физическая состоятельность'],
		['geotechnical_transfer', 'Перенос в геотехнику']
	];

	const links = $derived(
		[
			item?.canonical_url ? { label: 'Ссылка', href: item.canonical_url } : null,
			item?.doi ? { label: `DOI ${item.doi}`, href: `https://doi.org/${item.doi}` } : null,
			item?.arxiv_id
				? { label: `arXiv ${item.arxiv_id}`, href: `https://arxiv.org/abs/${item.arxiv_id}` }
				: null
		].filter(Boolean) as Array<{ label: string; href: string }>
	);
</script>

<svelte:head><title>{item?.title ?? 'Публикация'} · Проксима</title></svelte:head>

<p class="muted small"><a href="/publications">← Публикации</a></p>

<div class="spread head">
	<h1>{item.title}</h1>
	<div class="row">
		<span class="pill pill-mute">{KIND[item.kind] ?? item.kind}</span>
		{#if item.is_preprint}<span class="pill pill-mute">препринт</span>{/if}
		{#if item.rank_total_score !== null && item.rank_total_score !== undefined}
			<span class="pill pill-good">оценка {Number(item.rank_total_score).toFixed(1)}</span>
		{:else}
			<span class="pill pill-warn">без оценки</span>
		{/if}
	</div>
</div>

<p class="muted meta">
	{#if item.authors}{item.authors} · {/if}
	{#if item.venue}{item.venue} · {/if}
	{item.publication_date ?? 'дата неизвестна'}
	{#if item.citation_count !== null && item.citation_count !== undefined}
		· цитирований {n(item.citation_count)}{/if}
	{#each links as link}
		· <a href={link.href} target="_blank" rel="noopener">{link.label} ↗</a>
	{/each}
</p>

<div class="two">
	<section class="panel">
		<header><h2>Аннотация</h2></header>
		<div class="body">
			{#if item.abstract}
				<p class="abstract">{item.abstract}</p>
			{:else}
				<p class="empty">Аннотации у источника не было.</p>
			{/if}
		</div>
	</section>

	<section class="panel">
		<header>
			<h2>Оценка</h2>
			<span class="muted">light-модель, шкала 0–10</span>
		</header>
		<div class="body">
			{#if ranking}
				<dl class="dims">
					{#each DIMENSIONS as [key, label]}
						<div>
							<dt class="muted">{label}</dt>
							<dd class="mono">{ranking[key] ?? '—'}</dd>
						</div>
					{/each}
					<div>
						<dt class="muted">итог</dt>
						<dd class="mono strong">{Number(item.rank_total_score ?? ranking.total_score ?? 0).toFixed(2)}</dd>
					</div>
				</dl>
				{#if ranking.reason}<p class="dim">{ranking.reason}</p>{/if}
				{#if ranking.categories?.length}
					<div class="row">
						{#each ranking.categories as category}
							<span class="pill pill-mute">{category}</span>
						{/each}
					</div>
				{/if}
				<p class="muted small">
					близость к профилю сбора {item.semantic_score !== null
						? Number(item.semantic_score).toFixed(3)
						: '—'}
					· keyword_score {item.keyword_score ?? '—'}
					{#if ranking.recommend_deep_analysis}· модель просила глубокий разбор{/if}
				</p>
			{:else}
				<p class="empty">
					Материал не оценивался: не дошёл до ранжировщика или тот упал. Смотрите логи прогона.
				</p>
			{/if}
		</div>
	</section>
</div>

{#if analysis}
	<section class="panel">
		<header>
			<h2>Глубокий разбор</h2>
			<span class="muted">heavy-модель</span>
		</header>
		<div class="body analysis">
			{#each SECTIONS as [key, label]}
				{#if analysis[key]}
					<h3>{label}</h3>
					<p>{analysis[key]}</p>
				{/if}
			{/each}
			{#if analysis.limitations?.length}
				<h3>Ограничения</h3>
				<ul>{#each analysis.limitations as row}<li>{row}</li>{/each}</ul>
			{/if}
			{#if analysis.research_ideas?.length}
				<h3>Идеи для исследований</h3>
				<ul>{#each analysis.research_ideas as row}<li>{row}</li>{/each}</ul>
			{/if}
			<p class="muted small">
				код {analysis.code_available ? 'доступен' : 'не заявлен'} · датасет {analysis.dataset_available
					? 'доступен'
					: 'не заявлен'}
			</p>
		</div>
	</section>
{/if}

<div class="two">
	<section class="panel">
		<header><h2>Источники</h2></header>
		{#if data.sources?.length}
			<div class="table-scroll">
				<table>
					<thead><tr><th>Источник</th><th>Внешний id</th><th>Впервые</th><th>Последний раз</th></tr></thead>
					<tbody>
						{#each data.sources as row}
							<tr>
								<td>{row.source}</td>
								<td class="mono small">{row.external_id}</td>
								<td class="muted small">{when(row.first_seen_at)}</td>
								<td class="muted small">{when(row.last_seen_at)}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{:else}
			<p class="empty">Записей об источниках нет.</p>
		{/if}
	</section>

	<section class="panel">
		<header><h2>Решения гейта</h2></header>
		{#if data.decisions?.length}
			<div class="table-scroll">
				<table>
					<thead><tr><th>Стадия</th><th>Решение</th><th class="num">keyword</th><th class="num">semantic</th><th>Причина</th></tr></thead>
					<tbody>
						{#each data.decisions as row}
							<tr>
								<td class="muted">{row.stage}</td>
								<td><Pill status={row.decision} map={DECISION} /></td>
								<td class="num mono small">{row.keyword_score ?? '—'}</td>
								<td class="num mono small">{row.semantic_score !== null && row.semantic_score !== undefined ? Number(row.semantic_score).toFixed(3) : '—'}</td>
								<td class="muted small">{row.reason || row.blocked_by || '—'}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{:else}
			<p class="empty">Журнал решений по этому материалу пуст.</p>
		{/if}
	</section>
</div>

<p class="muted small">
	В корпусе с {when(item.created_at)} · обновлён {when(item.updated_at)} · id
	<span class="mono">{item.id}</span>
</p>

<style>
	.two {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(420px, 1fr));
		gap: var(--gap);
		margin-bottom: var(--gap);
	}

	.head h1 {
		max-width: 80ch;
		line-height: 1.3;
	}

	.meta {
		margin: -4px 0 16px;
		max-width: 100ch;
	}

	.abstract {
		line-height: 1.6;
		white-space: pre-line;
	}

	.dims {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
		gap: 10px;
		margin: 0 0 12px;
	}

	.dims div {
		display: grid;
		gap: 2px;
	}

	.dims dt {
		font-size: 12px;
	}

	.dims dd {
		margin: 0;
		font-size: 18px;
	}

	.strong {
		color: var(--accent);
	}

	.analysis h3 {
		font-size: 13px;
		margin: 14px 0 4px;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--muted);
	}

	.analysis p,
	.analysis li {
		line-height: 1.55;
		max-width: 90ch;
	}

	.small {
		font-size: 12.5px;
	}
</style>
