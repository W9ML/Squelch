/** FontAwesome global setup. Import once (from providers) before any icon
 *  renders so the core stylesheet is present and Next doesn't double-inject it. */
import { config } from "@fortawesome/fontawesome-svg-core";
import "@fortawesome/fontawesome-svg-core/styles.css";

config.autoAddCss = false;
