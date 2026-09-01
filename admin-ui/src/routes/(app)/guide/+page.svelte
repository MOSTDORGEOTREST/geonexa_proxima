<script lang="ts">
	let { data } = $props();

	const sections = $derived(data.guide?.sections ?? []);
</script>

<svelte:head><title>Как писать профиль · Проксима</title></svelte:head>

<h1>Как писать профиль интересов</h1>

<p class="muted lead">
	Профиль выглядит как свободный текст, но обрабатывается механически: описание режется на темы,
	каждая тема ищется по корпусу отдельно, а явные темы вдобавок сверяются с текстом статьи
	буквально. Ни одно из этих правил из поля ввода не видно, и ошибка в профиле не падает — она
	молча портит выдачу. Ниже правила и разбор типичных ошибок; та же инструкция приходит в
	Telegram по команде <code>/howto</code>.
</p>

{#if sections.length}
	{#each sections as section, index}
		<section class="panel">
			<header><h2>{index + 1}. {section.title}</h2></header>
			<div class="body">
				{#each section.body as paragraph}
					<p>{paragraph}</p>
				{/each}

				{#if section.good?.length}
					<h3 class="good-title">Так работает</h3>
					{#each section.good as example}
						<pre class="sample good">{example}</pre>
					{/each}
				{/if}

				{#if section.bad?.length}
					<h3 class="bad-title">Так не работает</h3>
					{#each section.bad as item}
						<pre class="sample bad">{item.example}</pre>
						<p class="reason muted">{item.reason}</p>
					{/each}
				{/if}
			</div>
		</section>
	{/each}
{:else}
	<section class="panel"><p class="empty">Инструкция недоступна: API не отвечает.</p></section>
{/if}

<style>
	.lead {
		max-width: 78ch;
	}

	h2 {
		font-size: 16px;
		margin: 0;
	}

	/* Абзацы и примеры разведены собственными полями, поэтому тело этой
	   панели — поток, а не сетка: иначе к полям добавится ещё и gap. */
	.body {
		display: block;
	}

	.body > :last-child {
		margin-bottom: 0;
	}

	h3 {
		font-size: 13px;
		margin: 14px 0 6px;
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}

	.good-title {
		color: var(--good);
	}

	.bad-title {
		color: var(--critical);
	}

	p {
		max-width: 78ch;
		margin: 0 0 10px;
	}

	.sample {
		max-width: 78ch;
		margin: 0 0 8px;
		padding: 10px 12px;
		border-radius: 6px;
		border-left: 3px solid var(--border);
		background: var(--surface-2);
		font-family: var(--font-m);
		font-size: 12.5px;
		white-space: pre-wrap;
		overflow-x: auto;
	}

	.sample.good {
		border-left-color: var(--good);
	}

	.sample.bad {
		border-left-color: var(--critical);
	}

	.reason {
		margin: -4px 0 12px;
		font-size: 12.5px;
	}
</style>
