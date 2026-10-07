/**
 * Copyright(c) Live2D Inc. All rights reserved.
 *
 * Use of this source code is governed by the Live2D Open Software license
 * that can be found at https://www.live2d.com/eula/live2d-open-software-license-agreement_en.html.
 */
import { csmVector } from '@framework/type/csmvector';
import { CubismMotionSyncContext } from './cubismmotionsyncenginelib';
import { CubismMotionSyncEngineMappingInfo } from './cubismmotionsyncenginemappinginfo';
import * as $ from './cubismmotionsyncutil';
export declare enum EngineType {
    EngineType_Cri = 0,
    EngineType_Unknown = 1
}
export declare class MotionSyncUtil {
    /**
     * @deprecated 非推奨になりました。代わりにCubismMath.fmodを使用してください。
     *
     * 浮動小数点の余りを求める。
     *
     * @param x 被除数（割られる値）
     * @param y 除数（割る値）
     * @returns 余り
     */
    static fmod(x: number, y: number): number;
}
export declare class MappingInfoListMapper {
    release(): void;
    /** 保留原生映射的拥有者，销毁时需要释放嵌套字符串和值数组而不只清空 JS 容器。 */
    setJObject(mappingInfoList: csmVector<CubismMotionSyncEngineMappingInfo>): void;
    ConvertObjectToNative(infoList: csmVector<CubismMotionSyncEngineMappingInfo>): void;
    /** 同时回收每项映射和连续结构缓冲，多次连接不能持续增长 WASM 堆。 */
    deleteMappingInfoList(): void;
    getMappingInfoListPtr(): number;
    private _infoBufferList;
    private _mappingInfoList;
    private _mappingInfoListFirstPtr;
}
export declare class MotionSyncContext {
    constructor(context: CubismMotionSyncContext, mapper: MappingInfoListMapper, cubismParameterCount: number);
    release(): void;
    getContext(): CubismMotionSyncContext;
    getMapper(): MappingInfoListMapper;
    getCubismParameterCount(): number;
    private _context;
    private _mapper;
    private _cubismParameterCount;
}
export declare namespace Live2DCubismMotionSyncFramework {
    const MotionSyncUtil: typeof $.MotionSyncUtil;
    type MotionSyncUtil = $.MotionSyncUtil;
    const MotionSyncContext: typeof $.MotionSyncContext;
    type MotionSyncContext = $.MotionSyncContext;
    const MappingInfoListMapper: typeof $.MappingInfoListMapper;
    type MappingInfoListMapper = $.MappingInfoListMapper;
}
