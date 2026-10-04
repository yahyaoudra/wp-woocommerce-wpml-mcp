<?php
/**
 * Plugin Name: CXG MCP Helper
 * Description: Read-only helper endpoints for the self-hosted WooCommerce/WPML MCP bridge.
 * Version: 1.0.0
 */
if (!defined('ABSPATH')) exit;

add_action('rest_api_init', function () {
    register_rest_route('cxg-mcp/v1', '/diagnostics', [
        'methods' => 'GET',
        'permission_callback' => function () { return current_user_can('manage_woocommerce'); },
        'callback' => function () {
            return [
                'ok' => true,
                'wordpress' => get_bloginfo('version'),
                'woocommerce' => defined('WC_VERSION') ? WC_VERSION : null,
                'wpml' => defined('ICL_SITEPRESS_VERSION') ? ICL_SITEPRESS_VERSION : null,
                'site_url' => get_site_url(),
            ];
        },
    ]);
    register_rest_route('cxg-mcp/v1', '/languages', [
        'methods' => 'GET',
        'permission_callback' => function () { return current_user_can('manage_woocommerce'); },
        'callback' => function () {
            if (!has_filter('wpml_active_languages')) {
                return new WP_Error('wpml_missing', 'WPML active languages filter is unavailable.', ['status' => 503]);
            }
            $langs = apply_filters('wpml_active_languages', null, ['skip_missing' => 0]);
            $out = [];
            foreach ((array)$langs as $code => $row) {
                $out[] = [
                    'code' => $code,
                    'name' => isset($row['translated_name']) ? $row['translated_name'] : ($row['native_name'] ?? $code),
                    'native_name' => $row['native_name'] ?? null,
                    'default_locale' => $row['default_locale'] ?? null,
                    'active' => !empty($row['active']),
                ];
            }
            return ['languages' => $out];
        },
    ]);
});
