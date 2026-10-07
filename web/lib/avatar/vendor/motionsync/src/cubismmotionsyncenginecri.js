import { csmVector } from '@framework/type/csmvector';
import { CubismLogWarning } from '@framework/utils/cubismdebug';
import { CubismMotionSyncProcessorCRI } from './cubismmotionsyncprocessorcri';
import { MappingInfoListMapper, MotionSyncContext } from './cubismmotionsyncutil';
import { DefaultAudioBitDepth, ICubismMotionSyncEngine } from './icubismmotionsyncengine';
import { MotionSyncContextConfig_CRI } from './motionsyncconfig_cri';
// Namespace definition for compatibility.
import * as $ from './motionsyncconfig_cri';
export const SampleRateMin = 16000;
export const SampleRateMax = 128000;
export class CubismMotionSyncEngineCri extends ICubismMotionSyncEngine {
    /** 原生上下文创建时读取配置，读取完成立即回收临时配置缓冲。 */
    CreateProcessor(cubismParameterCount, mappingInfoList, sampleRate) {
        if (this.isClosed()) {
            CubismLogWarning("[CubismMotionSyncEngineCri.CreateProcessor] Cubism MotionSync Engine is not started.'");
            return null;
        }
        if (mappingInfoList.getSize() < 1) {
            CubismLogWarning("[CubismMotionSyncEngineCri.CreateProcessor] mappingInfoList size is invalid.'");
            return null;
        }
        if (!(SampleRateMin <= sampleRate && sampleRate <= SampleRateMax)) {
            CubismLogWarning("[CubismMotionSyncEngineCri.CreateProcessor] sampleRate is invalid.'");
            return null;
        }
        const contextConfig = new MotionSyncContextConfig_CRI(sampleRate, DefaultAudioBitDepth);
        const mapper = new MappingInfoListMapper();
        mapper.setJObject(mappingInfoList);
        const context = this.getEngineHandle().createContext(this.getType(), contextConfig, mapper, mappingInfoList.getSize());
        contextConfig.releaseNativeArray();
        const contextHandle = new MotionSyncContext(context, mapper, cubismParameterCount);
        const processor = new CubismMotionSyncProcessorCRI(this, contextHandle, mappingInfoList, sampleRate, DefaultAudioBitDepth);
        this._processors.pushBack(processor);
        return processor;
    }
    constructor(engineHandle, type, name, version) {
        super(engineHandle, type, name, version);
        this._processors = new csmVector();
    }
}
// eslint-disable-next-line @typescript-eslint/no-namespace
export var Live2DCubismMotionSyncFramework;
(function (Live2DCubismMotionSyncFramework) {
    Live2DCubismMotionSyncFramework.MotionSyncContextConfig_CRI = $.MotionSyncContextConfig_CRI;
})(Live2DCubismMotionSyncFramework || (Live2DCubismMotionSyncFramework = {}));
