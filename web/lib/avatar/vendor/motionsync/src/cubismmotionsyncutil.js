/**
 * Copyright(c) Live2D Inc. All rights reserved.
 *
 * Use of this source code is governed by the Live2D Open Software license
 * that can be found at https://www.live2d.com/eula/live2d-open-software-license-agreement_en.html.
 */
import { csmVector } from '@framework/type/csmvector';
import { MappingInfoStructSize, } from './cubismmotionsyncenginemappinginfo';
// Namespace definition for compatibility.
import * as $ from './cubismmotionsyncutil';
var ToPointer = Live2DCubismMotionSyncCore.ToPointer;
export var EngineType;
(function (EngineType) {
    EngineType[EngineType["EngineType_Cri"] = 0] = "EngineType_Cri";
    EngineType[EngineType["EngineType_Unknown"] = 1] = "EngineType_Unknown";
})(EngineType || (EngineType = {}));
export class MotionSyncUtil {
    /**
     * @deprecated 非推奨になりました。代わりにCubismMath.fmodを使用してください。
     *
     * 浮動小数点の余りを求める。
     *
     * @param x 被除数（割られる値）
     * @param y 除数（割る値）
     * @returns 余り
     */
    static fmod(x, y) {
        return Number((x - Math.floor(x / y) * y).toPrecision(8));
    }
}
export class MappingInfoListMapper {
    // デストラクタ
    release() {
        this.deleteMappingInfoList();
    }
    /** 保留原生映射的拥有者，销毁时需要释放嵌套字符串和值数组而不只清空 JS 容器。 */
    setJObject(mappingInfoList) {
        this.deleteMappingInfoList();
        this._mappingInfoList = mappingInfoList;
        this._infoBufferList = new csmVector();
        this.ConvertObjectToNative(mappingInfoList);
    }
    ConvertObjectToNative(infoList) {
        const infoListCount = infoList.getSize();
        for (let index = 0; index < infoListCount; index++) {
            this._infoBufferList.pushBack(infoList.at(index).toNativeArray(true));
        }
        // メモリ確保
        let mappingInfoListPtr = ToPointer.Malloc(this._infoBufferList.at(0).length *
            infoListCount *
            this._infoBufferList.at(0).BYTES_PER_ELEMENT);
        // 先頭アドレスを保存
        this._mappingInfoListFirstPtr = mappingInfoListPtr;
        // メモリ上で1列に並べる
        for (let infoListIndex = 0; infoListIndex < infoListCount; infoListIndex++) {
            // 要素の数分回す
            for (let infoElementIndex = 0; infoElementIndex < MappingInfoStructSize; infoElementIndex++) {
                if (infoElementIndex == 4) {
                    // Floatの値渡しなのでここだけこのようにする
                    ToPointer.AddValuePtrFloat(mappingInfoListPtr, infoElementIndex * Float32Array.BYTES_PER_ELEMENT, this._infoBufferList.at(infoListIndex)[infoElementIndex]);
                }
                else {
                    ToPointer.AddValuePtrInt32(mappingInfoListPtr, infoElementIndex * Float32Array.BYTES_PER_ELEMENT, this._infoBufferList.at(infoListIndex)[infoElementIndex]);
                }
            }
            // 利用したバイト数分ポインタを進める
            mappingInfoListPtr += MappingInfoStructSize * Float32Array.BYTES_PER_ELEMENT;
        }
    }
    /** 同时回收每项映射和连续结构缓冲，多次连接不能持续增长 WASM 堆。 */
    deleteMappingInfoList() {
        var _a;
        if (!this._infoBufferList) {
            return;
        }
        for (let index = 0; index < ((_a = this._mappingInfoList) === null || _a === void 0 ? void 0 : _a.getSize()); index++) {
            this._mappingInfoList.at(index).releaseNativeArray();
        }
        ToPointer.Free(this._mappingInfoListFirstPtr);
        this._mappingInfoListFirstPtr = 0;
        this._mappingInfoList = null;
        this._infoBufferList.clear();
        this._infoBufferList = void 0;
        this._infoBufferList = null;
    }
    getMappingInfoListPtr() {
        return this._mappingInfoListFirstPtr;
    }
}
export class MotionSyncContext {
    constructor(context, mapper, cubismParameterCount) {
        this._context = context;
        this._mapper = mapper;
        this._cubismParameterCount = cubismParameterCount;
    }
    release() {
        var _a, _b;
        (_a = this._context) === null || _a === void 0 ? void 0 : _a.csmMotionSyncDelete();
        this._context = void 0;
        this._context = null;
        (_b = this._mapper) === null || _b === void 0 ? void 0 : _b.release();
        this._mapper = void 0;
        this._mapper = null;
        this._cubismParameterCount = 0;
    }
    getContext() {
        return this._context;
    }
    getMapper() {
        return this._mapper;
    }
    getCubismParameterCount() {
        return this._cubismParameterCount;
    }
}
// eslint-disable-next-line @typescript-eslint/no-namespace
export var Live2DCubismMotionSyncFramework;
(function (Live2DCubismMotionSyncFramework) {
    Live2DCubismMotionSyncFramework.MotionSyncUtil = $.MotionSyncUtil;
    Live2DCubismMotionSyncFramework.MotionSyncContext = $.MotionSyncContext;
    Live2DCubismMotionSyncFramework.MappingInfoListMapper = $.MappingInfoListMapper;
})(Live2DCubismMotionSyncFramework || (Live2DCubismMotionSyncFramework = {}));
