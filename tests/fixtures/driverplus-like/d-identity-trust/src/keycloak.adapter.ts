export class KeycloakAdapter {
  constructor(private readonly config: any) {}
  connect() {
    const url = this.config.get('KEYCLOAK_URL');
    const realmP1 = this.config.get('KEYCLOAK_REALM_P1');
    const realmP2 = this.config.get('KEYCLOAK_REALM_P2');
    const adminClient = this.config.get('KEYCLOAK_ADMIN_CLIENT_ID');
    const adminSecret = this.config.get('KEYCLOAK_ADMIN_CLIENT_SECRET');
    return {url, realmP1, realmP2, adminClient, adminSecret};
  }
}
