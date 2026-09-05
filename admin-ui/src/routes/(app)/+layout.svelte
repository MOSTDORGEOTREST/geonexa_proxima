<script lang="ts">
	import { page } from '$app/stores';
	import { SECTIONS, entryOf, hasTabs, isActive, sectionOf } from '$lib/nav';

	let { data, children } = $props();

	const current = $derived(sectionOf($page.url.pathname));
	const tabs = $derived(hasTabs(current) ? (current?.items ?? []) : []);

	/** Счётчик заявок один на всю админку: он и на разделе, и на вкладке. */
	const pending = $derived(Number(data.pending ?? 0));
	const sectionPending = (section: (typeof SECTIONS)[number]): number =>
		pending && section.items.some((item) => item.pending) ? pending : 0;

	function toggleTheme(): void {
		const root = document.documentElement;
		const next = root.dataset.theme === 'light' ? 'dark' : 'light';
		root.dataset.theme = next;
		// Год: тема — не сессионное решение, переспрашивать каждый вход незачем.
		document.cookie = `theme=${next}; path=/; max-age=31536000; samesite=lax`;
	}
</script>

<header>
	<div class="wrap bar">
		<a href="/" class="brand">
			<img src="/brand/gx-emblem.svg" alt="" width="26" height="26" />
			<span class="name">ПРОКСИМА</span>
		</a>
		<nav aria-label="Разделы">
			{#each SECTIONS as section}
				<a
					href={entryOf(section)}
					class:active={current?.id === section.id}
					aria-current={current?.id === section.id ? 'page' : undefined}
				>
					{section.label}
					{#if sectionPending(section)}<b class="badge">{sectionPending(section)}</b>{/if}
				</a>
			{/each}
		</nav>
		<div class="right">
			{#if data.me?.environment && data.me.environment !== 'production'}
				<span class="pill pill-warn">{data.me.environment}</span>
			{/if}
			<button type="button" onclick={toggleTheme} title="Сменить тему" aria-label="Сменить тему"
				>◐</button
			>
			<form method="POST" action="/logout">
				<button type="submit" class="link">{data.me?.username ?? 'выход'} · выйти</button>
			</form>
		</div>
	</div>

	<!-- Второй уровень появляется только там, где в разделе больше одной
	     страницы. Пустая полоска под шапкой на дашборде и на сборе съедала бы
	     высоту и обещала выбор, которого нет. -->
	{#if tabs.length}
		<div class="tabs-strip">
			<nav class="wrap tabs" aria-label={current?.label}>
				{#each tabs as item}
					<a
						href={item.href}
						class:active={isActive(item.href, $page.url.pathname)}
						aria-current={isActive(item.href, $page.url.pathname) ? 'page' : undefined}
					>
						{item.label}
						{#if item.pending && pending}<b class="badge">{pending}</b>{/if}
					</a>
				{/each}
			</nav>
		</div>
	{/if}
</header>

<main class="wrap">{@render children()}</main>

<style>
	.badge {
		display: inline-block;
		min-width: 17px;
		padding: 0 5px;
		margin-left: 5px;
		border-radius: 9px;
		background: var(--warning);
		color: #1a1a1a;
		font-size: 11px;
		line-height: 17px;
		text-align: center;
	}

	header {
		border-bottom: 1px solid var(--border);
		background: var(--surface);
		position: sticky;
		top: 0;
		z-index: 10;
	}

	.bar {
		display: flex;
		align-items: center;
		gap: 16px;
		height: 46px;
	}

	.brand {
		display: inline-flex;
		align-items: center;
		gap: 9px;
		text-decoration: none;
		flex: none;
	}

	.name {
		font-family: var(--font-d);
		font-weight: 800;
		letter-spacing: 0.06em;
		font-size: 15px;
	}

	nav {
		display: flex;
		gap: 4px;
		flex: 1;
		overflow-x: auto;
		scrollbar-width: none;
	}

	nav a {
		display: inline-flex;
		align-items: center;
		padding: 4px 10px;
		border-radius: var(--r-pill);
		text-decoration: none;
		color: var(--text-dim);
		font-size: 13.5px;
		white-space: nowrap;
	}

	nav a:hover {
		background: var(--surface-2);
	}

	nav a.active {
		color: var(--text);
		background: color-mix(in srgb, var(--accent) 16%, transparent);
	}

	/* Второй уровень отличается от первого не размером, а способом выделения:
	   заливка у разделов, подчёркивание у вкладок. Две одинаковые полосы пилюль
	   друг под другом читались бы как один длинный список — ровно то, от чего
	   уходили. */
	.tabs-strip {
		border-top: 1px solid var(--border-soft);
		background: color-mix(in srgb, var(--surface-2) 55%, transparent);
	}

	.tabs {
		display: flex;
		gap: 2px;
		height: 32px;
		align-items: stretch;
		overflow-x: auto;
		scrollbar-width: none;
	}

	.tabs a {
		display: inline-flex;
		align-items: center;
		gap: 2px;
		padding: 0 12px;
		border-radius: 0;
		border-bottom: 2px solid transparent;
		color: var(--muted);
		font-size: 13px;
		text-decoration: none;
		white-space: nowrap;
	}

	.tabs a:hover {
		background: transparent;
		color: var(--text);
	}

	.tabs a.active {
		color: var(--text);
		background: transparent;
		border-bottom-color: var(--accent);
	}

	.right {
		display: flex;
		align-items: center;
		gap: 10px;
		flex: none;
	}

	.right button {
		padding: 5px 10px;
	}

	.link {
		border: none;
		color: var(--muted);
		font-size: 12.5px;
		padding: 5px 8px;
	}

	.link:hover {
		color: var(--text);
	}

	main {
		padding: 14px 0 48px;
		display: grid;
		gap: 12px;
	}

	/* На узком экране бренд и правый блок несжимаемы, и навигации остаётся
	   щель в десяток пикселей: пять разделов превращаются в невидимую
	   прокрутку. Отдаём разделам всю ширину второй строкой. */
	@media (max-width: 900px) {
		.bar {
			flex-wrap: wrap;
			height: auto;
			padding: 8px 0;
			gap: 8px 12px;
		}

		nav {
			order: 3;
			flex-basis: 100%;
		}

		.name {
			font-size: 14px;
		}
	}
</style>
