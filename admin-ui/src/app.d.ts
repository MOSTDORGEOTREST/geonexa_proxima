declare global {
	namespace App {
		interface Locals {
			token: string | null;
			username: string | null;
		}
	}
}

export {};
