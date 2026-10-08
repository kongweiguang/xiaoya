import java.awt.image.BufferedImage;
import java.io.ByteArrayOutputStream;
import java.lang.reflect.Constructor;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import javax.imageio.ImageIO;

/** 原画作为唯一纹理，通过连续网格保留接缝与光泽；不再用重新绘制的零件拼出另一张脸。 */
public final class ExportConceptXiaoya {
    private static final String MODEL = "org.umamo.runtime.model.";
    private static final int SIZE = 1280;
    private static final int STEP = 16;
    private static final int POINTS = SIZE / STEP + 1;
    private static Object channels, white, black;
    private static final List<String> MORPHS = List.of(
        "ParamAngleX", "ParamAngleY",
        "ParamEyeBallX", "ParamEyeBallY", "ParamBrowLY", "ParamBrowRY",
        "ParamMouthForm", "ParamLeafSwing", "ParamArmL", "ParamArmR",
        "ParamBreath", "ParamBrowLAngle", "ParamBrowRAngle", "ParamEyeSmile"
    );

    /** 验证模式只读现有工程；生成模式写独立目录，正式切换留到 Core 和浏览器检查之后。 */
    public static void main(String[] args) throws Exception {
        if (args[0].equals("--verify")) { verifyEditable(Path.of(args[1])); return; }
        Path source = Path.of(args[0]).toAbsolutePath();
        Path output = Path.of(args[1]).toAbsolutePath();
        Files.createDirectories(output);
        BufferedImage image = ImageIO.read(source.toFile());
        if (image.getWidth() != image.getHeight() || !image.getColorModel().hasAlpha())
            throw new IllegalArgumentException("Expected the original transparent square concept");
        int pixels = image.getWidth();
        channels = make(MODEL + "ChannelGrids", java.util.Map.of());
        white = make(MODEL + "ColorRgb", 1f, 1f, 1f);
        black = make(MODEL + "ColorRgb", 0f, 0f, 0f);
        float[] positions = grid();
        float[] uvs = positions.clone();
        for (int index = 0; index < uvs.length; index++) uvs[index] /= SIZE;
        Object mesh = make(MODEL + "DrawableMesh", positions, uvs, triangles());
        Object geometry = faceGrid(positions);
        List<Object> bindings = new ArrayList<>();
        for (String parameter : MORPHS) bindings.add(binding(parameter, positions));
        Object drawable = make(MODEL + "Drawable", "Concept", "小芽原画", "ConceptTilt",
            enumeration(MODEL + "BlendMode", "Normal"), List.of(), mesh, geometry, channels,
            100f, 1f, white, black, false, enumeration(MODEL + "AlphaBlendMode", "Over"),
            false, true, true, null, 0, "xiaoya/concept", bindings);
        Object placement = make(MODEL + "AtlasPlacement", 0, 0f, 0f, 1f, 1f, 0f);
        Object reference = make(MODEL + "SourceLayerRef", "xiaoya-concept", "Concept", true);
        Object tile = make(MODEL + "AtlasTile", "xiaoya/concept", "小芽原画", pixels, pixels, placement, reference, false, null);
        Object atlas = make(MODEL + "PuppetAtlas", List.of(make(MODEL + "AtlasPage", pixels, pixels)),
            List.of(tile), true, make(MODEL + "AtlasComposition", 1, 2));
        Object inventory = make(MODEL + "ArtSourceLayer", "Concept", "小芽原画", "Xiaoya", 0, 0,
            pixels, pixels, true, true, null, false, false, false);
        Object art = make(MODEL + "ArtSource", "xiaoya-concept", "concept.png", "concept.png", "png",
            List.of(inventory), null, System.currentTimeMillis(), 0, 0);
        Object renderRoot = make(MODEL + "RenderGroup", null, 500,
            List.of(make(MODEL + "RenderDrawable", "Concept")), channels, null);
        Object puppet = make(MODEL + "PuppetModel", parameters(), List.of(), List.of(
            rotation("ConceptBody", null, "ParamBodyAngleX", 640f, 990f, 1.2f),
            rotation("ConceptTilt", "ConceptBody", "ParamAngleZ", 0f, -245f, 2f)),
            List.of(drawable), List.of(make(MODEL + "OrgChild$Drawable", "Concept")), null,
            List.of(), renderRoot, List.of(), List.of(), (float) SIZE, (float) SIZE,
            SIZE / 2f, -SIZE / 2f, SIZE / 2f, enumeration(MODEL + "RuntimeTarget", "Cubism50"),
            false, atlas, List.of(art));
        Object options = make("org.umamo.interop.moc3.Moc3ExportOptions", false, false, false, true, true, true, null);
        Object lowered = call(instance("org.umamo.interop.moc3.export.Moc3Export"), "toMocDocument",
            puppet, enumeration("org.umamo.format.moc3.moc.MocVersion", "V50"), null, options);
        Files.write(output.resolve("xiaoya.moc3"), (byte[]) call(instance("org.umamo.format.moc3.Moc3"),
            "write", call(lowered, "getDocument")));
        Files.copy(source, output.resolve("texture.png"), java.nio.file.StandardCopyOption.REPLACE_EXISTING);
        Files.copy(source, output.resolve("poster.png"), java.nio.file.StandardCopyOption.REPLACE_EXISTING);
        Object raster = raster(image);
        Class<?> function = Class.forName("kotlin.jvm.functions.Function1");
        Object supplier = Proxy.newProxyInstance(function.getClassLoader(), new Class<?>[]{function},
            /** 工程嵌入同一份原画，使离开工作区后仍能编辑且不丢失像素来源。 */
            (proxy, method, arguments) -> method.getName().equals("invoke") ? raster : null);
        Object page = make("org.umamo.interop.cmo3.Cmo3Conversion$AtlasPage", png(image), pixels, pixels, raster);
        Object editable = call(instance("org.umamo.interop.cmo3.Cmo3Conversion"), "freshCmo3", puppet,
            List.of(page), java.util.Map.of("Concept", 0), "小芽 · concept 原画绑定",
            System.currentTimeMillis(), 0, supplier, raster);
        Files.write(output.resolve("concept-rig.cmo3"), (byte[]) call(instance("org.umamo.format.cmo3.Cmo3"),
            "write", call(editable, "getModel")));
        System.out.println("Concept rig: " + (positions.length / 2) + " vertices, 19 parameters; "
            + call(lowered, "getReport"));
    }

    /** 独立读回制作工程可发现缺失绑定，但不将格式读回冒充官方 Editor 打开和导出。 */
    private static void verifyEditable(Path source) throws Exception {
        Object cmo = call(instance("org.umamo.format.cmo3.Cmo3"), "read", Files.readAllBytes(source));
        Object puppet = call(instance("org.umamo.interop.cmo3.Cmo3Import"), "fromModelSource", call(cmo, "getRoot"), false);
        List<?> meshes = (List<?>) call(puppet, "getDrawables");
        List<?> parameters = (List<?>) call(puppet, "getParameters");
        List<?> deformers = (List<?>) call(puppet, "getDeformers");
        if (meshes.size() != 1 || parameters.size() != 19 || deformers.size() != 2)
            throw new IllegalStateException("Concept CMO counts differ from runtime contract");
        List<?> bindings = (List<?>) call(meshes.get(0), "getBlendShapes");
        List<?> cells = (List<?>) call(call(meshes.get(0), "getGeometryGrid"), "getCells");
        if (bindings.size() != 14 || cells.size() != 12) throw new IllegalStateException("CMO geometry is incomplete");
        System.out.println("CMO readback: 1 mesh, 19 parameters, 2 rotations, 14 morphs, 12 face combinations; official Editor not verified");
    }

    /** 16 像素间距平衡局部形变和移动端成本，共用网格不撕开肩颈接缝。 */
    private static float[] grid() {
        float[] positions = new float[POINTS * POINTS * 2];
        for (int row = 0; row < POINTS; row++) for (int column = 0; column < POINTS; column++) {
            int index = (row * POINTS + column) * 2;
            positions[index] = column * STEP;
            positions[index + 1] = row * STEP;
        }
        return positions;
    }

    /** 共用顶点使区域间保持连续，三角形绕序与现行官方 WebGL 渲染器一致。 */
    private static int[] triangles() {
        int[] result = new int[(POINTS - 1) * (POINTS - 1) * 6];
        int index = 0;
        for (int row = 0; row < POINTS - 1; row++) for (int column = 0; column < POINTS - 1; column++) {
            int a = row * POINTS + column, b = a + 1, c = a + POINTS, d = c + 1;
            for (int vertex : new int[]{a, b, d, a, d, c}) result[index++] = vertex;
        }
        return result;
    }

    /** 眨眼和开口保留完整组合；其余参数独立叠加，避免十九维关键形态产生指数级文件。 */
    private static Object faceGrid(float[] base) throws Exception {
        List<Object> axes = List.of(
            make(MODEL + "KeyformAxis", "ParamEyeLOpen", new float[]{0f, 1f}),
            make(MODEL + "KeyformAxis", "ParamEyeROpen", new float[]{0f, 1f}),
            make(MODEL + "KeyformAxis", "ParamMouthOpenY", new float[]{0f, 0.5f, 1f}));
        List<Object> cells = new ArrayList<>();
        for (int mouth = 0; mouth < 3; mouth++) for (int right = 0; right < 2; right++) for (int left = 0; left < 2; left++) {
            float[] delta = new float[base.length];
            for (int index = 0; index < base.length; index += 2) {
                float x = base[index], y = base[index + 1];
                delta[index] = -640f;
                delta[index + 1] = -745f + squash(x, y, 463f, 499f, 62f, 78f, 104f, 132f, left)
                    + squash(x, y, 789f, 499f, 62f, 78f, 104f, 132f, right)
                    + mouthDelta(x, y, mouth / 2f);
            }
            cells.add(make(MODEL + "KeyformCell", new int[]{left, right, mouth}, make(MODEL + "MeshDeltaForm", delta)));
        }
        return make(MODEL + "KeyformGrid", axes, cells);
    }

    /** 中心区域近乎闭合，外围平滑过渡；边界仍使用原画，避免黑色贴片或新绘嘴唇。 */
    private static float squash(float x, float y, float cx, float cy, float rx, float ry, float outerX, float outerY, float open) {
        return (y - cy) * (-0.965f * (1f - open)) * window(x - cx, rx, outerX) * window(y - cy, ry, outerY);
    }

    /** 闭嘴仍保留柔和笑弧；张口到原画姿态时弧线修正为零，不改变参考图的像素位置。 */
    private static float mouthDelta(float x, float y, float open) {
        float ratio = (x - 626f) / 55f;
        float curve = 5f * Math.max(0f, 1f - ratio * ratio) * window(y - 576f, 33f, 86f);
        return squash(x, y, 626f, 576f, 55f, 33f, 90f, 86f, open) + curve * (1f - open);
    }

    /** 平滑窗口只移动原画网格，不引入 CSS/SVG 近似造型或跨区域漂移。 */
    private static float window(float distance, float inner, float outer) {
        float value = Math.abs(distance);
        if (value <= inner) return 1f;
        if (value >= outer) return 0f;
        float t = (value - inner) / (outer - inner);
        return 1f - t * t * (3f - 2f * t);
    }

    /** 附加形态相对静音基线，嘴形横展受实际开口门控，不能撑开已闭合的嘴。 */
    private static Object binding(String id, float[] base) throws Exception {
        boolean positive = id.equals("ParamBreath") || id.equals("ParamEyeSmile");
        float range = id.matches("Param(Angle[XYZ]|BodyAngleX)") ? 30f : 1f;
        float[] keys = positive ? new float[]{0f, 1f} : new float[]{-range, 0f, range};
        float[] rest = new float[base.length];
        for (int index = 0; index < base.length; index += 2) {
            rest[index] = -640f;
            rest[index + 1] = -745f + mouthDelta(base[index], base[index + 1], 0f);
        }
        List<Object> forms = new ArrayList<>();
        for (float key : keys) {
            if (key == 0f) { forms.add(null); continue; }
            float[] positions = rest.clone();
            for (int index = 0; index < base.length; index += 2) {
                float[] shift = offset(id, base[index], base[index + 1], key / range);
                positions[index] += shift[0];
                positions[index + 1] += shift[1];
            }
            forms.add(make(MODEL + "MeshForm", positions, 100f, 1f, white, black));
        }
        List<Object> limits = new ArrayList<>();
        if (id.equals("ParamMouthForm")) limits.add(limit("ParamMouthOpenY"));
        if (id.equals("ParamEyeSmile") || id.startsWith("ParamEyeBall")) {
            limits.add(limit("ParamEyeLOpen"));
            limits.add(limit("ParamEyeROpen"));
        }
        return make(MODEL + "BlendShapeBinding", id, keys, positive ? 0 : 1, forms, limits);
    }

    /** SDK 的形态权重限制使闭眼优先于笑眼、静音优先于嘴形，无需浏览器补造业务状态。 */
    private static Object limit(String parameter) throws Exception {
        return make(MODEL + "BlendWeightLimit", parameter, List.of(
            make(MODEL + "BlendWeightLimitPoint", 0f, 0f),
            make(MODEL + "BlendWeightLimitPoint", 1f, 1f)));
    }

    /** 全局轻摆在压眼与口型之后做刚性旋转，避免叠加位移把闭眼附近的小三角形翻转。 */
    private static Object rotation(String id, String parent, String parameter, float x, float y, float degrees) throws Exception {
        List<Object> cells = new ArrayList<>();
        for (int index = 0; index < 3; index++) cells.add(make(MODEL + "KeyformCell", new int[]{index},
            make(MODEL + "RotationPivotForm", x, y, (index - 1) * degrees, 1f)));
        Object grid = make(MODEL + "KeyformGrid", List.of(make(MODEL + "KeyformAxis", parameter,
            new float[]{-30f, 0f, 30f})), cells);
        return make(MODEL + "Deformer$Rotation", id, id, parent, null, 0f, grid, channels,
            1f, white, black, false, false, true, true, true, List.of());
    }

    /** 动作采用克制幅度的连续位移场；既保留原画比例，也约束高亮和关节附近的拉伸。 */
    private static float[] offset(String id, float x, float y, float value) {
        float dx = 0f, dy = 0f;
        float head = window(y - 420f, 310f, 400f);
        float face = window(x - 626f, 300f, 420f) * window(y - 485f, 170f, 240f);
        float eyes = window(x - 463f, 60f, 98f) * window(y - 499f, 78f, 128f)
            + window(x - 789f, 60f, 98f) * window(y - 499f, 78f, 128f);
        switch (id) {
            case "ParamAngleX" -> dx = value * (7f * head + 8f * face);
            case "ParamAngleY" -> dy = -value * (8f * head + 5f * face);
            case "ParamEyeBallX" -> dx = value * 4f * eyes;
            case "ParamEyeBallY" -> dy = -value * 3f * eyes;
            case "ParamEyeSmile" -> dy = -(y - 499f) * value * 0.45f * eyes;
            case "ParamMouthForm" -> dx = (x - 626f) * value * 0.22f * window(x - 626f, 55f, 88f) * window(y - 576f, 33f, 80f);
            case "ParamBrowLY", "ParamBrowRY", "ParamBrowLAngle", "ParamBrowRAngle" -> {
                float cx = id.startsWith("ParamBrowL") ? 462f : 795f;
                float weight = window(x - cx, 43f, 75f) * window(y - 379f, 17f, 30f);
                dy = (id.endsWith("Angle") ? (x - cx) * 0.18f : -7f) * value * weight;
            }
            case "ParamBreath" -> {
                float weight = window(x - 636f, 170f, 245f) * window(y - 910f, 90f, 175f);
                dx = (x - 636f) * value * 0.004f * weight;
                dy = (y - 745f) * value * 0.007f * weight;
            }
            case "ParamLeafSwing" -> {
                float weight = window(y - 170f, 110f, 230f);
                float side = x < 640f ? 1f : -1f;
                float leaf = window(x - (x < 640f ? 210f : 1060f), 105f, 165f) * weight;
                dx = -(y - 370f) * value * side * 0.045f * leaf;
                dy = (x - (x < 640f ? 282f : 979f)) * value * side * 0.045f * leaf;
            }
            case "ParamArmL", "ParamArmR" -> {
                float cx = id.endsWith("L") ? 388f : 860f;
                float pivot = id.endsWith("L") ? 452f : 808f;
                float weight = window(x - cx, 47f, 105f) * window(y - 935f, 75f, 162f);
                float angle = value * (id.endsWith("L") ? 0.10f : -0.10f);
                dx = -(y - 796f) * angle * weight;
                dy = (x - pivot) * angle * weight;
            }
            default -> throw new IllegalArgumentException("Unknown parameter: " + id);
        }
        return new float[]{dx, dy};
    }

    /** 使用现有十九参数契约，替换原画不需要修改语音分析和应用层事件。 */
    private static List<Object> parameters() throws Exception {
        List<Object> result = new ArrayList<>();
        for (String id : List.of("ParamAngleZ", "ParamBodyAngleX"))
            result.add(make(MODEL + "Parameter", id, id, -30f, 30f, 0f,
                enumeration(MODEL + "ParameterKind", "NORMAL"), false));
        for (String id : MORPHS) {
            float range = id.matches("Param(Angle[XYZ]|BodyAngleX)") ? 30f : 1f;
            boolean positive = id.equals("ParamBreath") || id.equals("ParamEyeSmile");
            result.add(make(MODEL + "Parameter", id, id, positive ? 0f : -range, range, 0f,
                enumeration(MODEL + "ParameterKind", "BLEND_SHAPE"), false));
        }
        for (String id : List.of("ParamEyeLOpen", "ParamEyeROpen", "ParamMouthOpenY"))
            result.add(make(MODEL + "Parameter", id, id, 0f, 1f, id.contains("Eye") ? 1f : 0f,
                enumeration(MODEL + "ParameterKind", "NORMAL"), false));
        return result;
    }

    /** 原画的 RGBA 原样嵌入编辑工程，透明边缘不经过重复预乘。 */
    private static Object raster(BufferedImage image) throws Exception {
        int width = image.getWidth(), height = image.getHeight();
        byte[] pixels = new byte[width * height * 4];
        int cursor = 0;
        for (int y = 0; y < height; y++) for (int x = 0; x < width; x++) {
            int value = image.getRGB(x, y);
            pixels[cursor++] = (byte)(value >> 16); pixels[cursor++] = (byte)(value >> 8);
            pixels[cursor++] = (byte)value; pixels[cursor++] = (byte)(value >> 24);
        }
        return make("org.umamo.format.raster.RasterImage", width, height, pixels);
    }

    /** PNG 只用于工程内嵌；网页纹理直接复制原文件以保留可核对的字节指纹。 */
    private static byte[] png(BufferedImage image) throws Exception {
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        ImageIO.write(image, "png", bytes);
        return bytes.toByteArray();
    }

    /** Kotlin 值类构造器使用正式发行 JAR，签名不匹配时立即停止，避免生成部分有效资源。 */
    private static Object make(String name, Object... arguments) throws Exception {
        for (Constructor<?> constructor : Class.forName(name).getDeclaredConstructors()) {
            if (constructor.getParameterCount() != arguments.length || !compatible(constructor.getParameterTypes(), arguments)) continue;
            constructor.setAccessible(true);
            return constructor.newInstance(arguments);
        }
        throw new IllegalArgumentException("No constructor: " + name + " / " + arguments.length);
    }

    /** 只调用开源工具的现有公共方法，不改写序列化或官方 Core。 */
    private static Object call(Object receiver, String name, Object... arguments) throws Exception {
        for (Method method : receiver.getClass().getMethods()) {
            if (method.getName().equals(name) && method.getParameterCount() == arguments.length && compatible(method.getParameterTypes(), arguments))
                return method.invoke(receiver, arguments);
        }
        throw new IllegalArgumentException("No method: " + name);
    }

    /** 单例字段由发行包提供，使制作工具与 Python 和前端运行依赖隔离。 */
    private static Object instance(String name) throws Exception { return Class.forName(name).getField("INSTANCE").get(null); }

    /** 使用具名枚举避免内部序号变化造成不兼容导出。 */
    @SuppressWarnings({"rawtypes", "unchecked"})
    private static Object enumeration(String name, String value) throws Exception { return Enum.valueOf((Class)Class.forName(name), value); }

    /** 反射参数按实际类型匹配；不能让整数误匹配浮点而产生难追踪的制作误差。 */
    private static boolean compatible(Class<?>[] types, Object[] values) {
        for (int index = 0; index < types.length; index++) {
            if (values[index] == null) { if (types[index].isPrimitive()) return false; }
            else if (types[index].isPrimitive()) {
                if ((types[index] == int.class && !(values[index] instanceof Integer))
                    || (types[index] == float.class && !(values[index] instanceof Float))
                    || (types[index] == long.class && !(values[index] instanceof Long))
                    || (types[index] == boolean.class && !(values[index] instanceof Boolean))) return false;
            } else if (!types[index].isInstance(values[index])) return false;
        }
        return true;
    }
}
