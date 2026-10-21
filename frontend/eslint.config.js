import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist", "node_modules"] },
  {
    files: ["**/*.{ts,tsx}"],
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    languageOptions: { ecmaVersion: 2022, globals: globals.browser },
    plugins: { "react-hooks": reactHooks },
    rules: {
      ...reactHooks.configs.recommended.rules,
      // Attacker-controlled text must only ever be rendered as text (Phase 6 XSS rule). These are the ways to
      // inject HTML or run strings as code; none of them is allowed anywhere in the dashboard.
      "no-restricted-syntax": [
        "error",
        { selector: "JSXAttribute[name.name='dangerouslySetInnerHTML']", message: "Render attacker data as text only." },
        { selector: "MemberExpression[property.name=/^(innerHTML|outerHTML)$/]", message: "Render attacker data as text only." },
        { selector: "CallExpression[callee.property.name='insertAdjacentHTML']", message: "Render attacker data as text only." },
        { selector: "CallExpression[callee.object.name='document'][callee.property.name='write']", message: "No document.write." },
      ],
      "no-eval": "error",
      "no-implied-eval": "error",
      "no-new-func": "error",
    },
  },
);
