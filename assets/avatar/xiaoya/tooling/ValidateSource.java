import java.lang.reflect.Method;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.LinkedHashMap;
import java.util.Map;

/** 分层源文件使用独立读回路径验证，避免只有运行 MOC 而没有可编辑制作资产。 */
public final class ValidateSource {
    /** 保留旧版十六参数验收；表现候选必须完整增加三参数，禁止半套绑定或替代官方 Editor 门。 */
    public static void main(String[] args) throws Exception {
        Path base = Path.of(args[0]);
        Object art = call(singleton("org.umamo.format.psd.PsdReader"), "read", Files.readAllBytes(base.resolve("xiaoya-layers.psd")));
        List<?> layers = (List<?>)call(art, "getLayers");
        if (layers.size() != 16 || !call(art, "getWidthPx").equals(1280) || !call(art, "getHeightPx").equals(1280)) throw new IllegalStateException("PSD geometry mismatch");
        List<String> manifest = Files.readAllLines(base.resolve("layers/layers.tsv"));
        Map<String, String[]> rowByName = new LinkedHashMap<>();
        for (String line : manifest.subList(1, manifest.size())) { String[] row = line.split("\t"); rowByName.put(row[0], row); }
        int alphaPixels = 0;
        for (Object layer : layers) {
            String name = (String)call(layer, "getName");
            Object raster = call(layer, "getRaster");
            byte[] pixels = (byte[])call(raster, "getRgba");
            int width = (Integer)call(raster, "getWidth");
            int height = (Integer)call(raster, "getHeight");
            String[] row = rowByName.get(name);
            if (row == null || width != Integer.parseInt(row[4]) || height != Integer.parseInt(row[5]) || pixels.length != width * height * 4) throw new IllegalStateException("PSD layer mismatch " + name);
            int opaque = 0;
            for (int index = 3; index < pixels.length; index += 4) if ((pixels[index] & 255) > 0) opaque++;
            if (opaque == 0) throw new IllegalStateException("Blank PSD layer " + name);
            alphaPixels += opaque;
            System.out.println("PSD layer=" + name + " dimensions=" + width + "x" + height + " alphaPixels=" + opaque);
        }
        Object cmo = call(singleton("org.umamo.format.cmo3.Cmo3"), "read", Files.readAllBytes(base.resolve("xiaoya.cmo3")));
        Object puppet = call(singleton("org.umamo.interop.cmo3.Cmo3Import"), "fromModelSource", call(cmo, "getRoot"), false);
        List<?> drawables = (List<?>)call(puppet, "getDrawables");
        List<?> parameters = (List<?>)call(puppet, "getParameters");
        List<?> deformers = (List<?>)call(puppet, "getDeformers");
        if (drawables.size() != 16 || (parameters.size() != 16 && parameters.size() != 19) || deformers.size() != 6) throw new IllegalStateException("CMO rig count mismatch");
        int keyforms = 0;
        for (Object drawable : drawables) {
            Object grid = call(drawable, "getGeometryGrid");
            keyforms += ((List<?>)call(grid, "getCells")).size();
        }
        List<?> images = (List<?>)call(cmo, "imageResources");
        int nonempty = 0;
        for (Object resource : images) {
            byte[] png = (byte[])call(cmo, "extractLayerPng", resource);
            if (png != null && png.length > 0) nonempty++;
        }
        if (nonempty < 16 || keyforms < 16) throw new IllegalStateException("Missing editable CMO images or keyforms");
        System.out.println("SOURCE VALID: psdLayers=" + layers.size() + " alphaPixels=" + alphaPixels + " cmoDrawables=" + drawables.size() + " cmoParameters=" + parameters.size() + " cmoDeformers=" + deformers.size() + " cmoKeyforms=" + keyforms + " embeddedImages=" + nonempty);
        if (args.length > 1) {
            Path report = Path.of(args[1]);
            Files.createDirectories(report.toAbsolutePath().getParent());
            Files.writeString(report, String.format("{\n  \"reader\": \"Umamo v0.4.0\",\n  \"psdLayers\": %d,\n  \"nontransparentPixels\": %d,\n  \"cmoDrawables\": %d,\n  \"cmoParameters\": %d,\n  \"cmoRotationDeformers\": %d,\n  \"cmoGeometryKeyforms\": %d,\n  \"cmoEmbeddedImages\": %d,\n  \"officialEditorVerificationIncluded\": false\n}\n", layers.size(), alphaPixels, drawables.size(), parameters.size(), deformers.size(), keyforms, nonempty));
        }
    }

    /** Kotlin 单例保持正式读取 API，验证工具没有修改其格式数据。 */
    private static Object singleton(String name) throws Exception { return Class.forName(name).getField("INSTANCE").get(null); }

    /** 读取实现类可能是包内类型，允许访问仅用于验证公开数据而非重建内部序列化。 */
    private static Object call(Object receiver, String name, Object... args) throws Exception {
        for (Method method : receiver.getClass().getMethods()) {
            if (!method.getName().equals(name) || method.getParameterCount() != args.length) continue;
            boolean matches = true;
            for (int index = 0; index < args.length; index++) {
                Class<?> type = method.getParameterTypes()[index];
                if (args[index] != null && !(type == boolean.class && args[index] instanceof Boolean) && !type.isInstance(args[index])) matches = false;
            }
            if (!matches) continue;
            method.setAccessible(true);
            return method.invoke(receiver, args);
        }
        throw new IllegalArgumentException("No method " + receiver.getClass().getName() + "." + name);
    }
}
