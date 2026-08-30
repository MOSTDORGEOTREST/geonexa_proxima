<script lang="ts">
	import { page } from '$app/stores';
	let { data, children } = $props();

	const NAV = [
		{ href: '/', label: 'Дашборд' },
		{ href: '/subscribers', label: 'Подписчики' },
		{ href: '/chats', label: 'Чаты' },
		{ href: '/subscriptions', label: 'Подписки' },
		{ href: '/harvest', label: 'Сбор' },
		{ href: '/deliveries', label: 'Доставки' },
		{ href: '/schedules', label: 'Расписания' },
		{ href: '/runs', label: 'Прогоны' },
		{ href: '/models', label: 'Модели' },
		{ href: '/settings', label: 'Настройки' },
		{ href: '/audit', label: 'Аудит' }
	];

	const active = (href: string): boolean =>
		href === '/' ? $page.url.pathname === '/' : $page.url.pathname.startsWith(href);

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
		<nav>
			{#each NAV as item}
				<a href={item.href} class:active={active(item.href)}>{item.label}</a>
			{/each}
		</nav>
		<div class="right">
			{#if data.me?.environment && data.me.environment !== 'production'}
				<span class="pill pill-warn">{data.me.environment}</span>
			{/if}
			<button type="button" onclick={toggleTheme} title="Сменить тему">◐</button>
			<form method="POST" action="/logout">
				<button type="submit" class="link">{data.me?.username ?? 'выход'} · выйти</button>
			</form>
		</div>
	</div>
</header>

<main class="wrap">{@render children()}</main>

<style>
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
		gap: 20px;
		height: 56px;
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
		padding: 6px 11px;
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
		padding: 22px 0 60px;
		display: grid;
		gap: 18px;
	}
</style>
